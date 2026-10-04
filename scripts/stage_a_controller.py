#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from acceptance_contract import must_acceptance_ids
from control_state import (
    _plan_contract_revision_credit_count,
    _plan_contract_revision_terminal_attempts,
)
from deterministic_dispatch import select_actions
from runtime_contract import verify_state as verify_runtime_server_state

POLL_DEFAULT = 0.5
SEMANTIC_TERMINAL_GRACE_SECONDS = 10.0
MAX_SEMANTIC_INFRASTRUCTURE_RETRIES = 3
SEMANTIC_RETRY_PROTOCOL = "v2-semantic-infrastructure-retry-v1"
HARNESS_ROOT = Path(__file__).resolve().parents[1]
ROOT_SESSION_PROTOCOL = "v2-root-session-v1"
EXECUTION_LEDGER_PROTOCOL = "v2-stage-a-controller-execution-ledger-v1"
EXECUTION_RECEIPT_PROTOCOL = "v2-stage-a-controller-execute-v2"
ACCEPTANCE_CONTEXT_PROTOCOL = "v2-acceptance-validator-context-v1"
ACCEPTANCE_REMEDIATION_PROTOCOL = "v2-final-acceptance-remediation-v1"
ACCEPTANCE_CONTEXT_MAX_FILE_BYTES = 16 * 1024
ACCEPTANCE_CONTEXT_MAX_ARTIFACT_BYTES = 64 * 1024
ACCEPTANCE_CONTEXT_MAX_AUX_BYTES = 32 * 1024
PLANNER_PROMPTS = {
    "fresh": """Create the first structured implementation plan for this project.

FIRST read .opencode-v2/ORIGINAL_TASK.md, .opencode-v2/ACCEPTANCE.md, and
.opencode-v2/CONTROL_CONTRACT.md. Inspect only project files needed to create a
concrete, dependency-aware plan.

Verify commands must test the Done-when behavior, not incidental presentation
text from a test runner. Use a runner's exit status plus behavioral assertions;
do not parse human-readable summaries such as unittest/pytest/npm-test wording
unless Done-when explicitly requires that output format.

Edit only .opencode-v2/IMPLEMENTATION_PLAN.structured.json. Follow the
implementation-planner protocol exactly. Do not create readiness markers,
generated IMPLEMENTATION_PLAN.md, test reports, or supervisor-owned runtime
state. Stop after the durable structured plan edit.""",
    "repair": """Repair structured implementation planning for this project.

This is a bounded repair, not a request to rewrite the plan. FIRST read
.opencode-v2/IMPLEMENTATION_PLAN.repair.json and
.opencode-v2/IMPLEMENTATION_PLAN.structured.json. Read the acceptance contract
only when the repair packet itself explicitly names an acceptance/Axxx/MUST
error. After the two canonical reads, you may inspect an existing concrete file
already owned by an affected leaf, once, when its current interface is needed
to repair that leaf. Do not read unrelated repository files.

Edit only .opencode-v2/IMPLEMENTATION_PLAN.structured.json. Preserve every
existing leaf key and list order: deliverable IDs and historical attempt
accounting must remain stable. Do not add, remove, or reorder leaves.

Treat the repair packet's affected_keys and error evidence as authoritative.
Repair only those leaves and the dependency references they name. Where a
consumer lacks prerequisites, assign ownership and fail-closed verification to
its existing direct producers; retain each leaf key and all unrelated plan
contracts. Do not fabricate data merely to satisfy a check.

For every affected behavioral Done-when, the repaired verify_command MUST
actually execute the relevant behavior. Syntax checks, file existence, grep,
or static source inspection alone are not sufficient for a behavioral
completion contract. Prefer invoking an existing owned helper/test artifact or
a small runtime command that fails closed on the required behavior. When a
one-line Python Verify must prove a specific exception type, prefer a stdlib
call such as unittest.TestCase().assertRaises(ExpectedError, callable, *args)
instead of multiline or semicolon-compressed try/except syntax. Treat a test
runner's human-readable summary as presentation, not contract data: use its exit
status and behavioral assertions unless Done-when explicitly requires that
summary/output format. Never reference a new verifier/helper path unless that
path is explicitly added to the affected leaf's owned_artifacts or is owned by
a declared dependency.

Preserve acceptance requirements and all unrelated valid durable work. Never
edit generated IMPLEMENTATION_PLAN.md or supervisor-owned runtime state.""",
    "continue": """Continue structured implementation planning for this project.

FIRST read .opencode-v2/ORIGINAL_TASK.md, then .opencode-v2/ACCEPTANCE.md,
.opencode-v2/CONTROL_CONTRACT.md, and .opencode-v2/IMPLEMENTATION_PLAN.structured.json.

Edit only .opencode-v2/IMPLEMENTATION_PLAN.structured.json. Continue from
durable state without weakening acceptance requirements or editing generated
IMPLEMENTATION_PLAN.md or supervisor-owned runtime state.""",
}
SEMANTIC_PROMPTS = {
    ("acceptance-planner", "fresh"): """Create the acceptance contract for this project.

FIRST read .opencode-v2/ORIGINAL_TASK.md. Edit only .opencode-v2/ACCEPTANCE.md.
Follow the acceptance-planner protocol, preserve the original user goal, and do
not create supervisor-owned readiness state. Stop after the durable contract edit.""",
    ("acceptance-planner", "repair"): """Repair the acceptance contract for this project.

FIRST read .opencode-v2/ORIGINAL_TASK.md, .opencode-v2/ACCEPTANCE.md, and the
canonical guard error file .opencode-v2/ACCEPTANCE.guard-errors.txt. Do not list
or probe the control directory and do not guess alternate guard-error names.
Edit only .opencode-v2/ACCEPTANCE.md. Follow the acceptance-planner protocol and
do not create supervisor-owned readiness state.""",
    ("reference-researcher", "foundation"): """REFERENCE_MODE: FOUNDATION
Build or resume only the compact external-reference foundation. Read
.opencode-v2/acceptance/reference-work.json if present and follow the
reference-researcher protocol. Do not expand scope beyond durable requirements.""",
    ("reference-researcher", "validation"): """REFERENCE_MODE: VALIDATION
Resolve exactly one durable validation item. Resume
.opencode-v2/acceptance/reference-work.json if an item is in progress;
otherwise resolve only the first missing external-reference item. Follow the
reference-researcher protocol and persist its required durable evidence.

CHECKPOINT FIRST: after the protocol's required initial control/evidence reads,
do not spend another long planning turn or perform extra project discovery.
Within the NEXT tool-bearing response, write
.opencode-v2/acceptance/reference-work.json with mode=validation,
status=in_progress, current_item set to the exact item being resolved, and the
authoritative source/request you intend to use. Keep reasoning before this
checkpoint under 500 words. Only after that durable checkpoint may you inspect
the minimum additional project artifacts or make web calls. If external work
cannot be completed, persist the exact blocker/result for this same item before
returning REFERENCE_PARTIAL.""",
    ("acceptance-validator", "final"): """Run final acceptance validation from the
controller-supplied canonical evidence packet below. The packet is the complete
evidence surface for this run. Do not discover or execute anything else.

Your FIRST tool call must create `.opencode-v2/acceptance-report.json`. Judge
every exact MUST Axxx against the packet BEFORE that write. Resolve all
evidence conflicts before committing; an accepted first write is immutable.
After it succeeds, do not reconsider its verdict: for a committed PASS return
only the exact bare token ACCEPTANCE_PASS; for committed FAIL return
ACCEPTANCE_FAIL with only the IDs already marked FAIL in that report.
A later contradictory terminal answer is a control-plane failure and cannot
edit or override the first report. If the packet does not prove a MUST,
record FAIL for that MUST instead of seeking more evidence. Follow the
acceptance-validator report schema exactly; model prose alone is never success.""",
}


class ControllerError(RuntimeError):
    pass


def require_current_runtime_contract(project: Path, base_url: str) -> dict:
    try:
        return verify_runtime_server_state(HARNESS_ROOT, project, base_url)
    except ValueError as exc:
        raise ControllerError(
            "OPENCODE_RUNTIME_CONTRACT_STALE; restart the integration server "
            f"with the current harness before dispatch: {exc}"
        ) from exc


def load_json(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ControllerError(f"{label} missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ControllerError(f"{label} invalid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ControllerError(f"{label} is not an object: {path}")
    return data


def decision_path(project: Path) -> Path:
    return project / ".opencode-v2" / "query" / "decision.json"


def shadow_path(project: Path) -> Path:
    return project / ".opencode-v2" / "query" / "deterministic-shadow.json"


def root_session_path(project: Path) -> Path:
    return project / ".opencode-v2" / "work" / "root-session.json"


def execution_ledger_path(project: Path) -> Path:
    return project / ".opencode-v2" / "work" / "stage-a-controller-executions.json"


def execution_lock_path(project: Path) -> Path:
    return project / ".opencode-v2" / "work" / "stage-a-controller.lock"


@contextlib.contextmanager
def execution_lock(project: Path):
    path = execution_lock_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_execution_ledger(project: Path) -> dict:
    path = execution_ledger_path(project)
    if not path.exists():
        return {
            "owner": "stage-a-controller",
            "protocol": EXECUTION_LEDGER_PROTOCOL,
            "executions": {},
        }
    data = load_json(path, "stage-a controller execution ledger")
    if data.get("owner") != "stage-a-controller":
        raise ControllerError("execution ledger owner is not stage-a-controller")
    if data.get("protocol") != EXECUTION_LEDGER_PROTOCOL:
        raise ControllerError(
            f"execution ledger protocol is not {EXECUTION_LEDGER_PROTOCOL}"
        )
    if not isinstance(data.get("executions"), dict):
        raise ControllerError("execution ledger executions is not an object")
    return data


def save_execution_ledger(project: Path, data: dict) -> None:
    path = execution_ledger_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, sort_keys=True, indent=2) + "\n"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        tmp.unlink(missing_ok=True)


def canonical_execution_action(action: dict) -> dict:
    if not isinstance(action, dict):
        raise ControllerError("execution action is not an object")
    agent = str(action.get("agent") or "")
    if str(action.get("kind") or "") == "run_final_tests":
        normalized = {
            "kind": "run_final_tests",
            "command": str(action.get("command") or ""),
        }
        if normalized["command"] != ".opencode-v2/bin/run-checks":
            raise ControllerError(f"invalid final-tests action: {action!r}")
        return normalized
    if agent == "implementation-planner":
        normalized = {
            "kind": str(action.get("kind") or ""),
            "agent": agent,
            "mode": str(action.get("mode") or ""),
        }
        if normalized["kind"] != "launch" or normalized["mode"] not in PLANNER_PROMPTS:
            raise ControllerError(f"invalid implementation-planner action: {action!r}")
        return normalized
    if (agent, str(action.get("mode") or "")) in SEMANTIC_PROMPTS:
        normalized = {
            "kind": str(action.get("kind") or ""),
            "agent": agent,
            "mode": str(action.get("mode") or ""),
        }
        if normalized["kind"] != "launch":
            raise ControllerError(f"invalid semantic action: {action!r}")
        return normalized
    normalized = {
        "kind": str(action.get("kind") or ""),
        "agent": agent,
        "deliverable": str(action.get("deliverable") or ""),
    }
    if normalized["kind"] != "launch" or not normalized["agent"] or not normalized["deliverable"]:
        raise ControllerError(f"invalid implementation execution action: {action!r}")
    if normalized["agent"] == "task-splitter":
        try:
            generation = int(action.get("generation"))
            claim = int(action.get("claim") or 1)
        except (TypeError, ValueError) as exc:
            raise ControllerError(
                "task-splitter launch lacks a valid generation/claim"
            ) from exc
        if generation < 1:
            raise ControllerError("task-splitter launch generation must be positive")
        if claim < 1:
            raise ControllerError("task-splitter launch claim must be positive")
        normalized["generation"] = generation
        normalized["claim"] = claim
    return normalized


def execution_action_id(
    state_version: str,
    root_session: str,
    action: dict,
    dispatch_generation: int | None = None,
    semantic_work_token: str = "",
    semantic_attempt_slot: int | None = None,
) -> str:
    payload = {
        "state_version": str(state_version),
        "root_session": str(root_session),
        "action": canonical_execution_action(action),
    }
    if dispatch_generation is not None:
        payload["dispatch_generation"] = int(dispatch_generation)
    if semantic_work_token:
        payload["semantic_work_token"] = str(semantic_work_token)
    if semantic_attempt_slot is not None:
        slot=int(semantic_attempt_slot)
        if slot < 1:
            raise ControllerError("semantic attempt slot must be positive")
        payload["semantic_attempt_slot"] = slot
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def prior_logical_implementation_intents(
    executions: dict,
    current_execution_id: str,
    root_session: str,
    action: dict,
    dispatch_generation: int,
    current_attempt_count: int,
) -> list[dict]:
    """Return prior intents for the same logical implementation attempt.

    state_version is deliberately excluded. The deterministic projection can
    advance while a just-posted native child is still materializing; that
    projection churn must not turn one logical attempt into a second dispatch.
    A genuine retry is distinguished by dispatch_generation, which advances
    only after terminal failure history is durable.
    """
    canonical = canonical_execution_action(action)
    matches = []
    for execution_id, intent in executions.items():
        if execution_id == current_execution_id or not isinstance(intent, dict):
            continue
        if str(intent.get("root_session") or "") != str(root_session):
            continue
        if intent.get("action") != canonical:
            continue
        try:
            generation = int(intent.get("dispatch_generation") or 0)
        except (TypeError, ValueError):
            continue
        if generation != int(dispatch_generation):
            continue
        baseline=int(
            (intent.get("baseline_attempt") or {}).get("count") or 0
        )
        if baseline not in {
            int(current_attempt_count),
            max(0,int(current_attempt_count)-1),
        }:
            continue
        if not intent.get("transport_may_have_been_attempted"):
            continue
        matches.append(intent)
    matches.sort(
        key=lambda item: int(item.get("created_at_ms") or 0),
        reverse=True,
    )
    return matches


def prior_logical_task_splitter_intents(
    executions: dict,
    current_execution_id: str,
    root_session: str,
    action: dict,
) -> list[dict]:
    """Return prior intents for the same logical split generation.

    A task-splitter action already carries its split generation. Projection
    state may advance while that native child/corrective lifecycle is still
    materializing; state-version churn must not create a second primary
    splitter for the same generation.
    """
    canonical=canonical_execution_action(action)
    matches=[]
    for execution_id,intent in executions.items():
        if execution_id==current_execution_id or not isinstance(intent,dict):
            continue
        if str(intent.get("root_session") or "")!=str(root_session):
            continue
        if intent.get("action")!=canonical:
            continue
        if not intent.get("transport_may_have_been_attempted"):
            continue
        matches.append(intent)
    matches.sort(
        key=lambda item:int(item.get("created_at_ms") or 0),
        reverse=True,
    )
    return matches


def planner_dispatch_generation(project: Path) -> int:
    """Monotonic planner dispatch generation across audited failure refunds."""
    path = project / ".opencode-v2" / "work" / "planner-restarts.json"
    if not path.exists():
        return 0
    data = load_json(path, "planner restart ledger")
    if data.get("owner") not in (None, "supervisor"):
        raise ControllerError("planner restart ledger owner is not supervisor")
    try:
        count = int(data.get("count") or 0)
    except (TypeError, ValueError) as exc:
        raise ControllerError("planner restart ledger count is invalid") from exc
    if count < 0:
        raise ControllerError("planner restart ledger count is negative")
    recoveries = data.get("infrastructure_recoveries") or []
    if not isinstance(recoveries, list):
        raise ControllerError("planner infrastructure recoveries are invalid")
    seen=set()
    for item in recoveries:
        if not isinstance(item, dict):
            raise ControllerError("planner infrastructure recovery entry is invalid")
        sid=str(item.get("session") or "")
        if (
            not sid or sid in seen
            or item.get("source") != "operator-controller"
            or not str(item.get("reason") or "").strip()
            or not item.get("timestamp")
        ):
            raise ControllerError("planner infrastructure recovery entry is invalid")
        seen.add(sid)
    return count + len(recoveries)


def attempt_failure_generation(project: Path, did: str) -> int:
    """Monotonic logical-dispatch generation after durable terminal outcomes.

    Normal/infrastructure terminal outcomes are represented in failure_history.
    A plan-contract revision is different: it supersedes a completed attempt and
    grants a replacement slot without adding another failure row. Count those
    audited contract transitions too so the replacement cannot collide with the
    pre-repair execution intent.
    """
    path = project / ".opencode-v2" / "work" / "attempts.json"
    if not path.exists():
        return 0
    data = load_json(path, "attempt ledger")
    if data.get("owner") not in (None, "supervisor"):
        raise ControllerError("attempt ledger owner is not supervisor")
    entry = (data.get("deliverables") or {}).get(did) or {}
    if not isinstance(entry, dict):
        raise ControllerError(f"attempt ledger entry is not an object for {did}")
    history = entry.get("failure_history") or []
    if not isinstance(history, list) or not all(isinstance(item, dict) for item in history):
        raise ControllerError(f"attempt ledger failure history invalid for {did}")
    try:
        count=int(entry.get("count") or 0)
    except (TypeError,ValueError) as exc:
        raise ControllerError(f"attempt ledger count invalid for {did}") from exc
    return len(history) + _plan_contract_revision_credit_count(entry,count)


def current_attempt_is_terminal(project: Path, did: str) -> bool:
    """Return true only when the current sequence has durable terminal state."""
    path=project/".opencode-v2"/"work"/"attempts.json"
    if not path.exists():
        return False
    data=load_json(path,"attempt ledger")
    entry=(data.get("deliverables") or {}).get(did) or {}
    if not isinstance(entry,dict):
        return False
    try:
        count=int(entry.get("count") or 0)
    except (TypeError,ValueError):
        return False
    if count<1:
        return False
    for row in entry.get("failure_history") or []:
        if not isinstance(row,dict):
            continue
        try:
            if int(row.get("attempt") or 0)==count:
                return True
        except (TypeError,ValueError):
            continue
    for row in entry.get("operator_retry_attempts") or []:
        if not isinstance(row,dict):
            continue
        try:
            sequence=int(row.get("sequence") or 0)
        except (TypeError,ValueError):
            continue
        if (
            sequence==count
            and row.get("state") in {
                "plan_contract_replacement","bad_plan_replacement",
                "infrastructure_abort","infrastructure_blocked",
            }
        ):
            return True
    if count in _plan_contract_revision_terminal_attempts(entry,count):
        return True
    ready=project/".opencode-v2"/"work"/f"{did}.ready"
    return ready.is_file()


def attempt_snapshot(project: Path, did: str) -> dict:
    path = project / ".opencode-v2" / "work" / "attempts.json"
    if not path.exists():
        return {"count": 0, "sessions": []}
    data = load_json(path, "attempt ledger")
    if data.get("owner") not in (None, "supervisor"):
        raise ControllerError("attempt ledger owner is not supervisor")
    entry = (data.get("deliverables") or {}).get(did) or {}
    if not isinstance(entry, dict):
        raise ControllerError(f"attempt ledger entry is not an object for {did}")
    try:
        count = int(entry.get("count") or 0)
    except (TypeError, ValueError) as exc:
        raise ControllerError(f"attempt ledger count invalid for {did}") from exc
    sessions = entry.get("sessions") or []
    if not isinstance(sessions, list) or not all(isinstance(x, str) for x in sessions):
        raise ControllerError(f"attempt ledger sessions invalid for {did}")
    return {"count": count, "sessions": list(sessions)}


def unwrap_http_data(value):
    if isinstance(value, dict) and "data" in value:
        return value.get("data")
    return value


def superseded_attempt_session_ids(project: Path, did: str) -> set[str]:
    """Sessions explicitly replaced by a supervisor-owned same-attempt recovery."""
    path=project/".opencode-v2"/"work"/"attempts.json"
    if not path.exists():
        return set()
    data=load_json(path,"attempt ledger")
    if data.get("owner") not in (None,"supervisor"):
        raise ControllerError("attempt ledger owner is not supervisor")
    entry=(data.get("deliverables") or {}).get(did) or {}
    if not isinstance(entry,dict):
        raise ControllerError(f"attempt ledger entry is not an object for {did}")
    history=entry.get("unmaterialized_dispatch_history") or []
    if not isinstance(history,list):
        raise ControllerError(
            f"unmaterialized dispatch history invalid for {did}"
        )
    result=set()
    for row in history:
        if not isinstance(row,dict):
            raise ControllerError(
                f"unmaterialized dispatch history row invalid for {did}"
            )
        sid=row.get("replaced")
        replacement=row.get("replacement")
        if (
            isinstance(sid,str) and sid
            and isinstance(replacement,str)
            and replacement.startswith("dispatch:")
        ):
            result.add(sid)
    return result


def child_snapshot(project: Path, base_url: str, root: str) -> list[dict]:
    status, body = http_json(
        "GET",
        workspace_url(base_url, f"/session/{urllib.parse.quote(root)}/children", project),
    )
    body = unwrap_http_data(body)
    if status != 200 or not isinstance(body, list):
        raise ControllerError(f"root children lookup failed status={status}")
    result = []
    for item in body:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id") or "")
        if sid:
            result.append(item)
    return result


def reconcile_execution_evidence(intent: dict, attempts: dict, children: list[dict]) -> dict | None:
    baseline_attempt = intent.get("baseline_attempt") or {}
    baseline_sessions = set(baseline_attempt.get("sessions") or [])
    current_sessions = set(attempts.get("sessions") or [])
    added_sessions = sorted(current_sessions - baseline_sessions)
    try:
        baseline_count = int(baseline_attempt.get("count") or 0)
        current_count = int(attempts.get("count") or 0)
    except (TypeError, ValueError):
        return {"kind": "attempt-ledger-invalid"}

    baseline_children = set(intent.get("baseline_child_ids") or [])
    root = str(intent.get("root_session") or "")
    action = intent.get("action") or {}
    agent = str(action.get("agent") or "")
    new_children = []
    for child in children:
        if not isinstance(child, dict):
            continue
        sid = str(child.get("id") or "")
        if not sid or sid in baseline_children:
            continue
        if str(child.get("parentID") or "") != root:
            continue
        if str(child.get("agent") or "") != agent:
            continue
        new_children.append(sid)

    bound = sorted(set(new_children).intersection(current_sessions))
    if bound:
        return {"kind": "bound-native-child", "sessions": bound}

    reservations = sorted(s for s in added_sessions if s.startswith("dispatch:"))
    if reservations:
        return {"kind": "preclaim-reservation", "sessions": reservations}

    materialized = sorted(s for s in added_sessions if not s.startswith("dispatch:"))
    if materialized:
        return {"kind": "attempt-session", "sessions": materialized}

    if new_children:
        return {"kind": "native-child", "sessions": sorted(new_children)}

    if current_count > baseline_count:
        return {
            "kind": "attempt-count-increase",
            "before": baseline_count,
            "after": current_count,
        }
    return None


def native_child_ids(intent: dict, children: list[dict]) -> list[str]:
    """Return only the new, correctly parented child sessions for one intent."""
    baseline = set(intent.get("baseline_child_ids") or [])
    root = str(intent.get("root_session") or "")
    action = intent.get("action") or {}
    agent = str(action.get("agent") or "")
    result = []
    for child in children:
        if not isinstance(child, dict):
            continue
        sid = str(child.get("id") or "")
        if (
            sid
            and sid not in baseline
            and str(child.get("parentID") or "") == root
            and str(child.get("agent") or "") == agent
        ):
            result.append(sid)
    return sorted(set(result))


def validate_materialization_output(stdout: str, sid: str) -> None:
    """Accept supervisor diagnostics around exactly one session-bound receipt."""
    lines=[line.strip() for line in str(stdout or "").splitlines() if line.strip()]
    receipts=[
        line for line in lines if line.startswith("DISPATCH_MATERIALIZED ")
    ]
    if len(receipts)!=1:
        raise ControllerError(
            "native child materialization returned unexpected output: "
            + str(stdout or "").strip()[:1600]
        )
    fields={}
    for token in receipts[0].split()[1:]:
        if "=" not in token:
            continue
        key,value=token.split("=",1)
        fields[key]=value
    if fields.get("session")!=sid:
        raise ControllerError(
            "native child materialization receipt session mismatch: "
            + receipts[0][:1600]
        )


def materialize_native_child(project: Path, base_url: str, sid: str, agent: str) -> None:
    """Bind an observed child to its preclaim through supervisor-owned state."""
    supervisor = Path(__file__).with_name("supervisor.py")
    env = dict(os.environ)
    root = supervisor.parent.parent
    env["V2_ROOT"] = str(root)
    env["V2_OPENCODE_BASE_URL"] = base_url.rstrip("/")
    env["V2_OPENCODE_SESSION_TABLE"] = "session"
    default_db = root / "xdg" / "data-v11831-a2" / "opencode" / "opencode.db"
    if not default_db.is_file():
        raise ControllerError(f"canonical Stage-A database is missing: {default_db}")
    env["V2_OPENCODE_DB"] = str(default_db)
    proc = subprocess.run(
        [
            sys.executable, str(supervisor), "--project", str(project),
            "--agent", agent, "--materialize-dispatch-child", sid,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        timeout=15,
    )
    if proc.returncode:
        raise ControllerError(
            "native child materialization failed: " + proc.stdout.strip()[:1600]
        )
    validate_materialization_output(proc.stdout,sid)


def bind_unbound_native_child(
    project: Path,
    base_url: str,
    intent: dict,
    did: str,
    agent: str,
    attempts: dict,
    children: list[dict],
) -> dict:
    """Bind exactly one observed native child to its durable preclaim.

    A native child can be visible before the plugin's materialization hook has
    run.  Reporting that child as replay evidence without binding it leaves a
    dispatch placeholder as the current attempt, which makes restart recovery
    unable to classify the real session.  Binding is safe only for the one
    child created by this intent and never creates another child or attempt.
    """
    current = set(str(s) for s in attempts.get("sessions") or [])
    unbound = [sid for sid in native_child_ids(intent, children) if sid not in current]
    if not unbound:
        return attempts
    if len(unbound) != 1:
        raise ControllerError(
            "AMBIGUOUS_EXECUTION multiple unbound native children observed: "
            + json.dumps(sorted(unbound))
        )
    materialize_native_child(project, base_url, unbound[0], agent)
    return attempt_snapshot(project, did)


def replay_receipt(intent: dict, evidence: dict) -> dict:
    return {
        "protocol": EXECUTION_RECEIPT_PROTOCOL,
        "state_version": intent["state_version"],
        "root_session": intent["root_session"],
        "action": intent["action"],
        "execution_id": intent["execution_id"],
        "transport": "prompt_async+SubtaskPart",
        "replay_suppressed": True,
        "reconciliation": evidence,
    }


def evaluate(project: Path) -> dict:
    decision = load_json(decision_path(project), "decision")
    state_version = str(decision.get("state_version") or "")
    if not state_version:
        raise ControllerError("decision.state_version missing")
    phase = str(decision.get("resume_phase") or "")
    actions = select_actions(decision)
    if not isinstance(actions, list) or not actions:
        raise ControllerError("selector returned no actions")
    scheduler=(
        decision.get("scheduler")
        if isinstance(decision.get("scheduler"),dict)
        else {}
    )
    return {
        "protocol": "v2-stage-a-controller-shadow-v1",
        "state_version": state_version,
        "resume_phase": phase,
        "actions": actions,
        # Keep replay-specific scheduler evidence attached to the exact
        # decision generation used for action selection. The controller must
        # not reread a newer decision later merely to recover this evidence.
        "scheduler": dict(scheduler),
    }


def compare_supervisor_shadow(project: Path, result: dict) -> tuple[bool, str]:
    shadow = load_json(shadow_path(project), "supervisor deterministic shadow")
    shadow_version = str(shadow.get("state_version") or "")
    shadow_actions = shadow.get("actions")
    if shadow_version != result["state_version"]:
        return False, (
            f"state-version-mismatch controller={result['state_version']} "
            f"supervisor={shadow_version}"
        )
    if shadow_actions != result["actions"]:
        return False, (
            "action-mismatch "
            f"controller={json.dumps(result['actions'], sort_keys=True)} "
            f"supervisor={json.dumps(shadow_actions, sort_keys=True)}"
        )
    return True, "exact-match"


def one_pass(
    project: Path,
    require_shadow: bool,
    shadow_attempts: int = 100,
    shadow_poll_seconds: float = 0.05,
) -> dict:
    attempts=max(1,int(shadow_attempts)) if require_shadow else 1
    last_detail=""
    for attempt in range(attempts):
        # Re-evaluate on every retry. The supervisor publishes decision.json and
        # deterministic-shadow.json as separate atomic replacements, so a
        # reader may briefly observe two adjacent coherent generations.
        result = evaluate(project)
        if not require_shadow:
            return result
        ok, detail = compare_supervisor_shadow(project, result)
        result["shadow_match"] = ok
        result["shadow_detail"] = detail
        if ok:
            return result
        last_detail=detail
        if attempt < attempts-1 and shadow_poll_seconds>0:
            time.sleep(shadow_poll_seconds)
    raise ControllerError(last_detail or "supervisor shadow mismatch")


def http_json(method: str, url: str, payload: dict | None = None, timeout: float = 5.0):
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = int(resp.status)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ControllerError(f"HTTP {exc.code} {method} {url}: {body[:1200]}") from exc
    except OSError as exc:
        raise ControllerError(f"HTTP {method} {url} failed: {exc}") from exc
    if not raw:
        return status, None
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw.decode("utf-8", errors="replace")


def workspace_url(base_url: str, path: str, project: Path) -> str:
    query = urllib.parse.urlencode({"directory": str(project)})
    return f"{base_url.rstrip('/')}{path}?{query}"


def root_session_from_execution_ledger(project: Path) -> str:
    ledger=load_execution_ledger(project)
    executions=ledger.get("executions") or {}
    roots={
        str(item.get("root_session") or "")
        for item in executions.values()
        if isinstance(item,dict) and str(item.get("root_session") or "")
    }
    if not roots:
        return ""
    if len(roots)!=1:
        raise ControllerError(
            "execution ledger contains conflicting root_session values"
        )
    return next(iter(roots))


def resolve_root_session(project: Path, base_url: str, explicit_session: str = "") -> str:
    if explicit_session:
        sid = explicit_session
    else:
        path=root_session_path(project)
        if path.exists():
            tracker = load_json(path, "root session tracker")
            if tracker.get("owner") != "supervisor":
                raise ControllerError("root session tracker owner is not supervisor")
            if tracker.get("protocol") != ROOT_SESSION_PROTOCOL:
                raise ControllerError(f"root session tracker protocol is not {ROOT_SESSION_PROTOCOL}")
            sid = str(tracker.get("session") or "")
            if not sid:
                raise ControllerError("root session tracker has no session")
        else:
            sid=root_session_from_execution_ledger(project)
            if not sid:
                raise ControllerError(f"root session tracker missing: {path}")

    status, info = http_json(
        "GET",
        workspace_url(base_url, f"/session/{urllib.parse.quote(sid)}", project),
    )
    if status != 200 or not isinstance(info, dict):
        raise ControllerError(f"root session lookup failed status={status}")
    agent = str(info.get("agent") or "")
    parent = info.get("parentID")
    directory = str(info.get("directory") or "")
    if agent != "transport-root":
        raise ControllerError(f"root session agent is {agent!r}, expected 'transport-root'")
    if parent:
        raise ControllerError(f"root session {sid} unexpectedly has parentID={parent}")
    if Path(directory).resolve() != project.resolve():
        raise ControllerError(
            f"root session directory mismatch: {directory!r} != {str(project)!r}"
        )
    return sid


def ensure_root_idle(project: Path, base_url: str, sid: str) -> None:
    status, data = http_json("GET", workspace_url(base_url, "/session/status", project))
    if status != 200 or not isinstance(data, dict):
        raise ControllerError(f"session status lookup failed status={status}")
    current = data.get(sid)
    if current is None:
        return
    if isinstance(current, dict):
        kind = str(current.get("type") or current.get("status") or "")
    else:
        kind = str(current)
    raise ControllerError(
        f"transport root is not idle: session={sid} status={kind or current!r}"
    )


def canonical_implementation_prompt(project: Path, agent: str, did: str) -> str:
    supervisor = Path(__file__).with_name("supervisor.py")
    cmd = [
        sys.executable,
        str(supervisor),
        "--project",
        str(project),
        "--agent",
        agent,
        "--render-dispatch-prompt",
        did,
    ]
    env = dict(os.environ)
    env.setdefault("V2_ROOT", str(supervisor.parent.parent))
    proc = subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    if proc.returncode:
        raise ControllerError(
            "canonical dispatch prompt render failed: " + proc.stdout.strip()[:1600]
        )
    prompt = proc.stdout.rstrip("\n")
    if not prompt.startswith(f"DELIVERABLE: {did}\n"):
        raise ControllerError(f"canonical dispatch prompt has unexpected shape for {did}")
    return prompt


# IMPORTANT: every Stage-A SubtaskPart injected into the technical root must
# omit the optional `command` field.  In pinned OpenCode v1.18.31 a truthy
# SubtaskPart.command causes handleSubtask() to append a synthetic parent
# message ("Summarize the task tool output above and continue with your task.")
# after the child completes.  The Stage-A root is transport-only
# (v2noop/root-noop), so parent continuation is never part of the protocol.
# Omitting command makes the native child completion terminal for that root
# turn and keeps the root available for the next deterministic SubtaskPart.
def build_implementation_subtask(action: dict, prompt: str) -> dict:
    agent = str(action.get("agent") or "")
    did = str(action.get("deliverable") or "")
    if not agent or not did:
        raise ControllerError("implementation launch action lacks agent/deliverable")
    if agent == "task-splitter":
        raise ControllerError("task-splitter is not enabled in implementation executor slice")
    return {
        "type": "subtask",
        "prompt": prompt,
        "description": f"Execute {did}",
        "agent": agent,
    }


def build_task_splitter_subtask(action: dict, split_request: dict) -> dict:
    """Build a no-tool splitter dispatch from its already-materialized request.

    A denied duplicate read can consume the finite native step budget even
    though the first result contains every fact needed for the proposal.
    Projecting the canonical, state-bound request into initial child context
    removes that mutable tool-use boundary; ownership remains project-relative.
    """
    agent = str(action.get("agent") or "")
    did = str(action.get("deliverable") or "")
    if agent != "task-splitter" or not did:
        raise ControllerError("task-splitter launch lacks canonical agent/deliverable")
    canonical = canonical_execution_action(action)
    if not isinstance(split_request, dict):
        raise ControllerError("task-splitter canonical split request is not an object")
    if str(split_request.get("parent_id") or "") != did:
        raise ControllerError("task-splitter canonical split request parent mismatch")
    try:
        request_generation = int(split_request.get("generation"))
    except (TypeError, ValueError) as exc:
        raise ControllerError("task-splitter canonical split request generation is invalid") from exc
    if request_generation != int(canonical["generation"]):
        raise ControllerError("task-splitter canonical split request generation mismatch")
    rendered = json.dumps(split_request, sort_keys=True, separators=(",", ":"))
    return {
        "type": "subtask",
        "prompt": (
            f"SPLIT_PARENT: {did}\n"
            "CANONICAL_SPLIT_REQUEST_JSON_BEGIN\n"
            f"{rendered}\n"
            "CANONICAL_SPLIT_REQUEST_JSON_END"
        ),
        "description": f"Split {did}",
        "agent": "task-splitter",
    }


def build_planner_subtask(action: dict) -> dict:
    canonical = canonical_execution_action(action)
    if canonical.get("agent") != "implementation-planner":
        raise ControllerError("planner action must use implementation-planner")
    mode = canonical["mode"]
    return {
        "type": "subtask",
        "prompt": PLANNER_PROMPTS[mode],
        "description": f"{mode.title()} implementation plan",
        "agent": "implementation-planner",
    }



def acceptance_must_ids(acceptance_text: str) -> list[str]:
    result = must_acceptance_ids(acceptance_text)
    if len(result) != len(set(result)):
        seen = set()
        duplicate = next(item for item in result if item in seen or seen.add(item))
        raise ControllerError(f"duplicate acceptance MUST id: {duplicate}")
    if not result:
        raise ControllerError("acceptance contract contains no exact MUST Axxx IDs")
    return result


def _safe_relative_project_path(project: Path, raw: str) -> Path:
    rel = Path(str(raw or ""))
    if not str(raw or "") or rel.is_absolute() or ".." in rel.parts:
        raise ControllerError(f"unsafe acceptance artifact path: {raw!r}")
    return rel


def _snapshot_project_artifact(
    project: Path,
    path: Path,
    artifact_budget: list[int],
) -> dict:
    root = project.resolve()
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ControllerError(f"acceptance artifact missing: {path}") from exc
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ControllerError(f"acceptance artifact escapes project: {path}") from exc

    shown_path = relative.as_posix()
    if resolved.is_dir():
        entries = []
        for child in sorted(resolved.iterdir(), key=lambda item: item.name)[:64]:
            try:
                entries.append(child.resolve().relative_to(root).as_posix())
            except (OSError, ValueError):
                continue
        return {
            "path": shown_path,
            "type": "directory",
            "entries": entries,
        }
    if not resolved.is_file():
        raise ControllerError(f"acceptance artifact is not a regular file: {path}")

    raw = resolved.read_bytes()
    item = {
        "path": shown_path,
        "type": "file",
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    if path.is_symlink():
        item["source_symlink"] = True
    if len(raw) > ACCEPTANCE_CONTEXT_MAX_FILE_BYTES:
        item["content_omitted"] = "file-too-large"
        return item
    if len(raw) > artifact_budget[0]:
        item["content_omitted"] = "packet-artifact-budget-exhausted"
        return item
    if b"\x00" in raw:
        item["content_omitted"] = "binary"
        return item
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        item["content_omitted"] = "non-utf8"
        return item
    artifact_budget[0] -= len(raw)
    item["content"] = content
    return item


def _owned_artifact_snapshots(project: Path, leaves: dict) -> list[dict]:
    root = project.resolve()
    budget = [ACCEPTANCE_CONTEXT_MAX_ARTIFACT_BYTES]
    seen: set[str] = set()
    snapshots: list[dict] = []
    for did in sorted(leaves):
        leaf = leaves[did]
        if not isinstance(leaf, dict):
            raise ControllerError(f"plan guard leaf is not an object: {did}")
        raw_paths = leaf.get("owned_artifact_paths") or []
        if not isinstance(raw_paths, list) or not all(isinstance(x, str) for x in raw_paths):
            raise ControllerError(f"owned artifact paths invalid for {did}")
        for raw_path in raw_paths:
            rel = _safe_relative_project_path(project, raw_path)
            pattern = rel.as_posix()
            if any(ch in pattern for ch in "*?["):
                matches = sorted(project.glob(pattern), key=lambda item: item.as_posix())
                if len(matches) > 64:
                    raise ControllerError(
                        f"acceptance artifact pattern has too many matches: {raw_path}"
                    )
                if not matches:
                    raise ControllerError(
                        f"acceptance artifact pattern has no matches: {raw_path}"
                    )
            else:
                matches = [project / rel]
            for match in matches:
                try:
                    key = match.resolve(strict=True).relative_to(root).as_posix()
                except (FileNotFoundError, ValueError) as exc:
                    raise ControllerError(
                        f"acceptance artifact path invalid for {did}: {match}"
                    ) from exc
                if key in seen:
                    continue
                seen.add(key)
                snapshots.append(_snapshot_project_artifact(project, match, budget))
    return snapshots


def _optional_acceptance_evidence(project: Path) -> dict:
    control = project / ".opencode-v2"
    candidates = [
        control / "reference-validation-gate.json",
        control / "acceptance" / "reference-work.json",
        control / "acceptance" / "reference-evidence.json",
        control / "browser-evidence.json",
    ]
    items_dir = control / "acceptance" / "reference-items"
    if items_dir.is_dir():
        candidates.extend(sorted(items_dir.glob("*.json"))[:32])

    remaining = ACCEPTANCE_CONTEXT_MAX_AUX_BYTES
    result = {}
    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        raw = path.read_bytes()
        rel = path.relative_to(project).as_posix()
        meta = {
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        if len(raw) <= remaining:
            try:
                meta["json"] = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                meta["content_omitted"] = "invalid-json-or-encoding"
            else:
                remaining -= len(raw)
        else:
            meta["content_omitted"] = "packet-aux-budget-exhausted"
        result[rel] = meta
    return result


def build_acceptance_validation_packet(project: Path) -> dict:
    project = project.resolve()
    control = project / ".opencode-v2"
    acceptance_path = control / "ACCEPTANCE.md"
    task_path = control / "ORIGINAL_TASK.md"
    try:
        acceptance_text = acceptance_path.read_text(encoding="utf-8")
        original_task = task_path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError) as exc:
        raise ControllerError(f"acceptance packet source unreadable: {exc}") from exc

    must_ids = acceptance_must_ids(acceptance_text)
    guard = load_json(control / "IMPLEMENTATION_PLAN.guard.json", "plan guard")
    leaves = guard.get("leaves")
    if not isinstance(leaves, dict) or not leaves:
        raise ControllerError("plan guard leaves missing for acceptance packet")

    test_report = load_json(control / "TEST_REPORT.json", "final test report")
    if (
        test_report.get("status") != "pass"
        or not isinstance(test_report.get("checks_run"), int)
        or isinstance(test_report.get("checks_run"), bool)
        or int(test_report.get("checks_run")) <= 0
        or test_report.get("checks_passed") != test_report.get("checks_run")
        or (test_report.get("missing_required_files") or [])
    ):
        raise ControllerError("final test report is not a clean PASS for acceptance packet")

    mapped: dict[str, list[str]] = {}
    leaf_evidence = []
    work = control / "work"
    for did in sorted(leaves):
        leaf = leaves[did]
        if not isinstance(leaf, dict):
            raise ControllerError(f"plan guard leaf invalid for acceptance packet: {did}")
        acceptance_ids = leaf.get("acceptance_ids") or []
        if not isinstance(acceptance_ids, list) or not all(
            isinstance(item, str) for item in acceptance_ids
        ):
            raise ControllerError(f"acceptance_ids invalid for {did}")
        for aid in acceptance_ids:
            mapped.setdefault(aid, []).append(did)

        ready = work / f"{did}.ready"
        if not ready.is_file() or ready.is_symlink():
            raise ControllerError(f"READY marker missing for acceptance leaf {did}")
        evidence = load_json(work / f"{did}.verify-evidence.json", f"verify evidence {did}")
        if evidence.get("owner") != "supervisor":
            raise ControllerError(f"verify evidence owner is not supervisor for {did}")
        latest = evidence.get("latest")
        if not isinstance(latest, dict):
            raise ControllerError(f"verify evidence latest missing for {did}")
        command = str(latest.get("command") or "")
        canonical_command = str(leaf.get("verify_command") or "")
        if not canonical_command or command != canonical_command:
            raise ControllerError(f"verify command provenance mismatch for {did}")
        if (
            latest.get("executed") is not True
            or latest.get("result") != "verified"
            or latest.get("exit_code") != 0
        ):
            raise ControllerError(f"leaf verify evidence is not a clean PASS for {did}")
        leaf_evidence.append({
            "id": did,
            "name": str(leaf.get("name") or ""),
            "role": str(leaf.get("role") or ""),
            "outcome": str(leaf.get("outcome") or ""),
            "done_when": str(leaf.get("done_when") or ""),
            "acceptance_ids": list(acceptance_ids),
            "owned_artifact_paths": list(leaf.get("owned_artifact_paths") or []),
            "verify": {
                "command": command,
                "executed": True,
                "exit_code": 0,
                "result": "verified",
                "timestamp": str(latest.get("timestamp") or ""),
                "stdout": str(latest.get("stdout") or "")[:4000],
                "stderr": str(latest.get("stderr") or "")[:4000],
            },
            "ready_sha256": hashlib.sha256(ready.read_bytes()).hexdigest(),
        })

    if set(mapped) != set(must_ids):
        raise ControllerError(
            "acceptance packet Axxx mapping mismatch: "
            f"missing={sorted(set(must_ids)-set(mapped))} "
            f"extra={sorted(set(mapped)-set(must_ids))}"
        )

    packet = {
        "protocol": ACCEPTANCE_CONTEXT_PROTOCOL,
        "project": str(project),
        "original_task": original_task,
        "acceptance_contract": acceptance_text,
        "must_ids": must_ids,
        "acceptance_to_leaves": {aid: sorted(mapped[aid]) for aid in must_ids},
        "final_test_report": test_report,
        "leaf_evidence": leaf_evidence,
        "artifact_snapshots": _owned_artifact_snapshots(project, leaves),
        "auxiliary_evidence": _optional_acceptance_evidence(project),
    }
    material = json.dumps(
        packet, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    packet["packet_sha256"] = hashlib.sha256(material).hexdigest()
    return packet


def _safe_reference_resume_url(raw) -> str:
    """Return a durable HTTP(S) resume URL only when it is safe to replay."""
    url=str(raw or "").strip()
    if not url or len(url)>2048 or any(ord(ch)<32 for ch in url):
        return ""
    try:
        parsed=urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() not in {"http","https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    sensitive=re.compile(
        r"(^|[_-])(?:api[_-]?key|key|token|secret|password|passwd|auth|"
        r"authorization|signature|sig)(?:$|[_-])",
        re.I,
    )
    try:
        query=urllib.parse.parse_qsl(parsed.query,keep_blank_values=True)
    except ValueError:
        return ""
    if any(sensitive.search(str(key)) for key,_ in query):
        return ""
    return url


def reference_resume_external_request_urls(
    project: Path,
    limit: int = 2,
) -> list[str]:
    """Read explicit, sanitized resume URLs from durable reference work state."""
    if limit < 1:
        return []
    path=project/".opencode-v2"/"acceptance"/"reference-work.json"
    if not path.is_file():
        return []
    try:
        work=load_json(path,"reference validation work")
    except ControllerError:
        return []
    result=[]
    seen=set()
    for field in (
        "last_successful_external_request_url",
        "next_external_request_url",
    ):
        url=_safe_reference_resume_url(work.get(field))
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(url)
        if len(result)>=limit:
            break
    return result


def build_semantic_subtask(action: dict, project: Path | None = None) -> dict:
    canonical = canonical_execution_action(action)
    key = (canonical.get("agent"), canonical.get("mode"))
    prompt = SEMANTIC_PROMPTS.get(key)
    if not prompt:
        raise ControllerError(f"unsupported semantic action: {canonical!r}")
    if key == ("reference-researcher", "validation") and project is not None:
        work_path=project/".opencode-v2"/"acceptance"/"reference-work.json"
        if work_path.exists():
            work=load_json(work_path,"reference validation work")
            current=str(work.get("current_item") or "").strip()
            if (
                work.get("mode")=="validation"
                and work.get("status")=="in_progress"
                and current
            ):
                next_action=str(work.get("next_action") or "").strip()
                authoritative=str(work.get("authoritative_source") or "").strip()
                prompt+=(
                    "\n\nRESUME CHECKPOINT ALREADY EXISTS:\n"
                    f"- current_item={current}\n"
                    "- Do NOT rewrite the initial checkpoint and do NOT repeat repository "
                    "discovery already summarized there.\n"
                    "- After the required control/evidence reads, the first non-control "
                    "action MUST be an authoritative web call or a durable evidence/result "
                    "write for this current_item.\n"
                    "- If the external step cannot be completed, persist the exact blocker "
                    "for this same current_item before returning REFERENCE_PARTIAL."
                )
                item_path=(
                    project/".opencode-v2"/"acceptance"/"reference-items"/
                    f"{current}.json"
                )
                if item_path.is_file():
                    prompt+=(
                        "\n- Existing current-item evidence exists at "
                        f".opencode-v2/acceptance/reference-items/{current}.json. "
                        "Read it during the control/evidence phase and reconcile "
                        "its IDs/names/claims against REFERENCE_FOUNDATION.md before "
                        "adding new external evidence."
                    )
                resume_urls=reference_resume_external_request_urls(
                    project,limit=2
                )
                if resume_urls:
                    prompt+=(
                        "\n- Durable external-request resume hints were explicitly "
                        "persisted by the prior researcher. Reuse them only when "
                        "they still match the current item and verified interface "
                        "contract:"
                    )
                    for index,url in enumerate(resume_urls,1):
                        prompt+=f"\n  - resume_request_{index}={url}"
                if authoritative:
                    prompt+=f"\n- authoritative_source={authoritative}"
                if next_action:
                    prompt+=f"\n- recorded_next_action={next_action}"
    if key == ("acceptance-validator", "final"):
        if project is None:
            raise ControllerError("final acceptance semantic subtask requires project")
        packet = build_acceptance_validation_packet(project)
        rendered = json.dumps(
            packet, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        prompt = (
            prompt
            + "\n\nCANONICAL_ACCEPTANCE_CONTEXT_JSON_BEGIN\n"
            + rendered
            + "\nCANONICAL_ACCEPTANCE_CONTEXT_JSON_END\n"
            + "Do not call read, list, glob, grep, bash, task, web, or question tools. "
            + "Your first tool call MUST write only "
            + "`.opencode-v2/acceptance-report.json` from this packet."
        )
    return {
        "type": "subtask",
        "prompt": prompt,
        "description": f"{canonical['agent']} {canonical['mode']}",
        "agent": canonical["agent"],
    }


def planner_reconcile_evidence(intent: dict, children: list[dict]) -> dict | None:
    baseline = set(intent.get("baseline_child_ids") or [])
    root = str(intent.get("root_session") or "")
    for child in children:
        if not isinstance(child, dict):
            continue
        sid = str(child.get("id") or "")
        if (
            sid and sid not in baseline
            and str(child.get("parentID") or "") == root
            and str(child.get("agent") or "") == "implementation-planner"
        ):
            return {"kind": "native-planner-child", "sessions": [sid]}
    return None


def semantic_reconcile_evidence(intent: dict, children: list[dict]) -> dict | None:
    ids=native_child_ids(intent, children)
    if not ids:
        return None
    by_id={
        str(child.get("id") or ""):child
        for child in children
        if isinstance(child,dict)
    }
    sid=ids[0]
    child=by_id.get(sid) or {}
    time_info=child.get("time") if isinstance(child.get("time"),dict) else {}
    updated=time_info.get("updated")
    evidence={"kind":"native-semantic-child","sessions":[sid]}
    try:
        updated_ms=int(updated)
    except (TypeError,ValueError):
        updated_ms=0
    if updated_ms>0:
        evidence["child_updated_at_ms"]=updated_ms
    return evidence


def session_is_active(project: Path, base_url: str, sid: str) -> bool:
    status, body = http_json("GET", workspace_url(base_url, "/session/status", project))
    if status != 200 or not isinstance(body, dict):
        raise ControllerError(f"session status lookup failed status={status}")
    return sid in body


def native_child_is_zero_work(child: dict) -> bool:
    """Require zero-token and zero-file evidence for a terminal child."""
    if not isinstance(child, dict):
        return False
    tokens=child.get("tokens") if isinstance(child.get("tokens"),dict) else {}
    summary=child.get("summary") if isinstance(child.get("summary"),dict) else {}
    try:
        token_total=sum(
            int(tokens.get(key) or 0)
            for key in ("input","output","reasoning")
        )
        file_total=sum(
            int(summary.get(key) or 0)
            for key in ("additions","deletions","files")
        )
    except (TypeError,ValueError):
        return False
    return token_total==0 and file_total==0


def replayable_superseded_orphans(
    project: Path,
    base_url: str,
    result: dict,
    intent: dict,
    children: list[dict],
    did: str,
) -> list[str]:
    """Return terminal native children explicitly superseded by recovery audit."""
    scheduler=(
        result.get("scheduler")
        if isinstance(result.get("scheduler"),dict)
        else {}
    )
    replayable=set(
        scheduler.get("replayable_reserved_deliverables") or []
    )
    if did not in replayable:
        return []
    superseded=superseded_attempt_session_ids(project,did)
    if not superseded:
        return []
    result_ids=[]
    for sid in native_child_ids(intent,children):
        if sid not in superseded:
            continue
        if session_is_active(project,base_url,sid):
            continue
        result_ids.append(sid)
    return sorted(set(result_ids))


def replayable_zero_work_orphans(
    project: Path,
    base_url: str,
    result: dict,
    intent: dict,
    attempts: dict,
    children: list[dict],
    did: str,
) -> list[str]:
    scheduler=(
        result.get("scheduler")
        if isinstance(result.get("scheduler"),dict)
        else {}
    )
    replayable=set(
        scheduler.get("replayable_reserved_deliverables") or []
    )
    if did not in replayable:
        return []
    sessions=(
        attempts.get("sessions")
        if isinstance(attempts,dict)
        else None
    )
    if not (
        isinstance(sessions,list)
        and sessions
        and isinstance(sessions[-1],str)
        and sessions[-1].startswith("dispatch:")
    ):
        return []
    current=set(str(s) for s in sessions)
    unbound=[
        sid for sid in native_child_ids(intent,children)
        if sid not in current
    ]
    if not unbound:
        return []
    by_id={
        str(child.get("id") or ""):child
        for child in children
        if isinstance(child,dict)
    }
    for sid in unbound:
        child=by_id.get(sid)
        if (
            not native_child_is_zero_work(child)
            or session_is_active(project,base_url,sid)
        ):
            return []
    return sorted(unbound)


def semantic_child_may_still_transition(
    project: Path, base_url: str, intent: dict, evidence: dict
) -> bool:
    sessions = evidence.get("sessions") if isinstance(evidence, dict) else []
    sid = str(sessions[0]) if isinstance(sessions, list) and sessions else ""
    if sid and session_is_active(project, base_url, sid):
        return True
    anchor=(
        evidence.get("child_updated_at_ms")
        if isinstance(evidence,dict)
        else None
    )
    if anchor is None:
        anchor=intent.get("created_at_ms")
    try:
        age=time.time()-(int(anchor)/1000.0)
    except (TypeError,ValueError):
        return False
    return age < SEMANTIC_TERMINAL_GRACE_SECONDS


def _atomic_write_json_path(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    payload=json.dumps(data,sort_keys=True,indent=2)+"\n"
    tmp=path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w",encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp,path)
    finally:
        tmp.unlink(missing_ok=True)



def _unique_verified_acceptance_failure_owner(
    ctrl: Path, leaves: dict, candidate_ids: list[str], check: dict
) -> str:
    """Resolve an ambiguous MUST only through one exact supervisor-verified command.

    A shared acceptance ID is never by itself permission to pick a worker.
    The failing check must reference an executable command that exactly
    matches one terminal executable leaf's independently completed Verify.
    """
    if len(candidate_ids)==1:
        return candidate_ids[0]
    if check.get("required_executable") is not True:
        return ""
    command=str(check.get("command") or "").strip()
    if not command:
        return ""
    owners=[]
    for did in candidate_ids:
        leaf=leaves.get(did)
        if not isinstance(leaf,dict):
            continue
        if leaf.get("split_children") or str(leaf.get("verify_command") or "").strip()!=command:
            continue
        path=ctrl/"work"/f"{did}.verify-evidence.json"
        if not path.is_file() or path.is_symlink():
            continue
        try:
            observed=load_json(path,"supervisor leaf Verify evidence")
        except ControllerError:
            continue
        latest=observed.get("latest")
        if not isinstance(latest,dict):
            continue
        if (
            latest.get("command")==command
            and latest.get("result")=="verified"
            and latest.get("executed") is True
            and latest.get("exit_code")==0
        ):
            owners.append(did)
    return owners[0] if len(owners)==1 else ""


def finalize_terminal_acceptance_failure_repair(
    project: Path, report: Path, report_data: dict
) -> dict:
    """Reopen uniquely owning READY leaves for a terminal acceptance FAIL."""
    ctrl=project/".opencode-v2"
    if report_data.get("protocol")!="v2-acceptance-report-v1":
        raise ControllerError("acceptance FAIL report protocol is invalid")
    if report_data.get("result")!="FAIL":
        raise ControllerError("acceptance remediation requires report.result=FAIL")
    try:
        must=acceptance_must_ids(
            (ctrl/"ACCEPTANCE.md").read_text(errors="replace")
        )
    except OSError as exc:
        raise ControllerError(
            f"acceptance remediation contract unavailable: {exc}"
        ) from exc

    checks=report_data.get("checks")
    if not isinstance(checks,list):
        raise ControllerError("acceptance FAIL report checks must be an array")
    by_id={}
    for item in checks:
        if not isinstance(item,dict):
            raise ControllerError("acceptance FAIL report contains invalid check")
        cid=str(item.get("id") or "")
        if cid in by_id:
            raise ControllerError(f"duplicate acceptance FAIL check {cid}")
        status=item.get("status")
        evidence=str(item.get("evidence") or "").strip()
        if status not in {"PASS","FAIL"} or len(evidence)<8:
            raise ControllerError(f"acceptance FAIL report invalid check {cid}")
        by_id[cid]=item
    if set(by_id)!=set(must):
        raise ControllerError(
            "acceptance FAIL report IDs do not exactly match MUST IDs"
        )
    failed=[cid for cid in must if by_id[cid].get("status")=="FAIL"]
    if not failed:
        raise ControllerError("acceptance FAIL report contains no failed MUST")

    manifest=load_json(
        ctrl/"IMPLEMENTATION_PLAN.guard.json","implementation manifest"
    )
    leaves=manifest.get("leaves")
    if not isinstance(leaves,dict):
        raise ControllerError("implementation manifest leaves are invalid")
    owners={}
    for cid in failed:
        matches=[
            did for did,leaf in leaves.items()
            if isinstance(leaf,dict)
            and cid in (leaf.get("acceptance_ids") or [])
        ]
        selected=_unique_verified_acceptance_failure_owner(
            ctrl,leaves,matches,by_id[cid]
        )
        if not selected:
            raise ControllerError(
                f"acceptance remediation owner must be unique for {cid}; "
                f"owners={matches}"
            )
        owners.setdefault(selected,[]).append(cid)

    report_sha=hashlib.sha256(report.read_bytes()).hexdigest()
    attempts=load_json(ctrl/"work"/"attempts.json","attempt ledger")
    deliverables=attempts.get("deliverables")
    if (
        attempts.get("owner") not in (None,"supervisor")
        or not isinstance(deliverables,dict)
    ):
        raise ControllerError("acceptance remediation attempt ledger is invalid")

    for did in owners:
        entry=deliverables.get(did)
        if not isinstance(entry,dict):
            raise ControllerError(
                f"acceptance remediation missing attempt entry for {did}"
            )
        try:
            count=int(entry.get("count") or 0)
            automatic_limit=int(entry.get("automatic_limit") or 0)
        except (TypeError,ValueError) as exc:
            raise ControllerError(
                f"acceptance remediation invalid attempt counters for {did}"
            ) from exc
        if count<1 or automatic_limit<1:
            raise ControllerError(
                f"acceptance remediation invalid attempt counters "
                f"deliverable={did} count={count} "
                f"automatic_limit={automatic_limit}"
            )
        rows=entry.get("acceptance_remediations") or []
        if not isinstance(rows,list):
            raise ControllerError(
                f"acceptance remediation ledger is malformed for {did}"
            )
        valid_rows=[
            row for row in rows
            if isinstance(row,dict)
            and row.get("source")=="stage-a-controller"
            and row.get("protocol")==ACCEPTANCE_REMEDIATION_PROTOCOL
            and row.get("grant")==1
        ]
        if count>=automatic_limit:
            if valid_rows:
                raise ControllerError(
                    f"ACCEPTANCE_REMEDIATION_EXHAUSTED "
                    f"deliverable={did} count={count} "
                    f"automatic_limit={automatic_limit}"
                )
            rows.append({
                "source":"stage-a-controller",
                "protocol":ACCEPTANCE_REMEDIATION_PROTOCOL,
                "grant":1,
                "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
                "attempt_count":count,
                "report_sha256":report_sha,
                "acceptance_ids":sorted(owners[did]),
            })
            entry["acceptance_remediations"]=rows
        ready=ctrl/"work"/f"{did}.ready"
        if not ready.is_file() or ready.is_symlink():
            raise ControllerError(
                f"acceptance remediation requires current READY leaf: {did}"
            )

    history={
        "owner":"stage-a-controller",
        "protocol":ACCEPTANCE_REMEDIATION_PROTOCOL,
        "report_sha256":report_sha,
        "failed_acceptance_ids":failed,
        "leaf_repairs":{},
        "created_at_ms":int(time.time()*1000),
    }
    for did,ids in sorted(owners.items()):
        leaf=leaves[did]
        evidence=[
            {
                "id":cid,
                "evidence":str(by_id[cid].get("evidence") or "").strip(),
            }
            for cid in ids
        ]
        history["leaf_repairs"][did]={
            "acceptance_ids":ids,
            "owned_artifact_paths":list(
                leaf.get("owned_artifact_paths") or []
            ),
            "evidence":evidence,
        }
        progress=ctrl/"work"/f"{did}.progress.md"
        lines=[
            "# Supervisor final-acceptance repair handoff",
            f"deliverable: {did}",
            f"source_protocol: {ACCEPTANCE_REMEDIATION_PROTOCOL}",
            f"acceptance_report_sha256: {report_sha}",
            "failed_acceptance_ids: "+",".join(ids),
            "",
            "Final acceptance found the following concrete contract miss(es).",
            "Repair only this leaf's owned artifacts; do not weaken Acceptance.",
        ]
        for item in evidence:
            lines.append(f"- {item['id']}: {item['evidence']}")
        lines.extend([
            "",
            "After the owned repair, run the exact packet Verify and return normally.",
            "",
        ])
        progress.write_text("\n".join(lines),encoding="utf-8")

    archive=(
        ctrl/"work"/"acceptance-remediation-history"/f"{report_sha}.json"
    )
    _atomic_write_json_path(archive,history)
    # Persist the bounded grant only after all remediation handoffs and their
    # audit archive exist. A pre-ledger I/O failure therefore cannot silently
    # consume the one automatic final-acceptance repair.
    _atomic_write_json_path(ctrl/"work"/"attempts.json",attempts)
    for did in owners:
        (ctrl/"work"/f"{did}.ready").unlink()
    (ctrl/"acceptance-pass.json").unlink(missing_ok=True)
    return {
        "kind":"terminal-acceptance-fail-remediation",
        "report_sha256":report_sha,
        "failed_acceptance_ids":failed,
        "reopened_deliverables":sorted(owners),
        "remediation_sha256":hashlib.sha256(archive.read_bytes()).hexdigest(),
    }


def reconcile_terminal_acceptance_with_supervisor(
    project: Path, base_url: str, sid: str
) -> str:
    """Bind terminal validator verdict/report history through supervisor state."""
    supervisor_path=Path(__file__).with_name("supervisor.py")
    env=dict(os.environ)
    root=supervisor_path.parent.parent
    env["V2_ROOT"]=str(root)
    env["V2_PROJECT"]=str(project)
    env["V2_OPENCODE_BASE_URL"]=base_url.rstrip("/")
    env["V2_OPENCODE_SESSION_TABLE"]="session"
    default_db=root/"xdg"/"data-v11831-a2"/"opencode"/"opencode.db"
    if not default_db.is_file():
        raise ControllerError(
            f"canonical Stage-A database is missing: {default_db}"
        )
    env["V2_OPENCODE_DB"]=str(default_db)
    proc=subprocess.run(
        [
            sys.executable,str(supervisor_path),
            "--project",str(project),
            "--reconcile-terminal-acceptance-validator",sid,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        timeout=30,
    )
    output=proc.stdout.strip()
    if proc.returncode:
        raise ControllerError(
            "ACCEPTANCE_TERMINAL_RECONCILE_FAILED "
            + (output.replace("\n"," ")[:1600] or f"rc={proc.returncode}")
        )
    if "ACCEPTANCE_VALIDATOR_TERMINAL_PASS_OK" in output:
        return "pass"
    if "ACCEPTANCE_VALIDATOR_TERMINAL_FAIL_RECOVERED" in output:
        return "fail-recovered"
    raise ControllerError(
        "ACCEPTANCE_TERMINAL_RECONCILE_INVALID "
        + output.replace("\n"," ")[:1600]
    )


def finalize_terminal_acceptance_report(
    project: Path, base_url: str, intent: dict, evidence: dict
) -> dict | None:
    """Reconcile terminal validator evidence, finalizing only an exact PASS.

    The bounded-subagent plugin clears acceptance artifacts immediately before
    every validator launch. Terminal recovery additionally binds the durable
    report to the child session's exact terminal verdict/tool history through
    supervisor.py before finalize-acceptance.py may mint a pass marker.
    """
    action = intent.get("action") if isinstance(intent, dict) else {}
    if action != {"kind": "launch", "agent": "acceptance-validator", "mode": "final"}:
        return None
    sessions=evidence.get("sessions") if isinstance(evidence,dict) else None
    if not isinstance(sessions,list) or len(sessions)!=1:
        raise ControllerError(
            "acceptance terminal recovery requires exactly one validator child"
        )
    sid=str(sessions[0] or "")
    if not sid:
        raise ControllerError("acceptance terminal recovery child id is empty")
    terminal_state=reconcile_terminal_acceptance_with_supervisor(
        project,base_url,sid
    )
    if terminal_state not in {"pass","fail-recovered"}:
        raise ControllerError(
            f"unexpected terminal acceptance state: {terminal_state}"
        )

    report = project / ".opencode-v2" / "acceptance-report.json"
    if not report.is_file() or report.is_symlink():
        return None
    try:
        created_ms = int(intent.get("created_at_ms") or 0)
        report_ms = report.stat().st_mtime_ns // 1_000_000
    except (OSError, TypeError, ValueError):
        return None
    if created_ms <= 0 or report_ms < created_ms:
        return None

    report_data=load_json(report,"acceptance report")
    result=str(report_data.get("result") or "")
    if result=="FAIL":
        return finalize_terminal_acceptance_failure_repair(
            project,report,report_data
        )
    if result!="PASS":
        raise ControllerError(
            f"acceptance report result must be PASS or FAIL, got {result!r}"
        )

    finalizer = HARNESS_ROOT / "scripts" / "finalize-acceptance.py"
    if not finalizer.is_file():
        raise ControllerError(f"acceptance finalizer missing: {finalizer}")
    proc = subprocess.run(
        [sys.executable, str(finalizer), str(project)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=900,
    )
    if proc.returncode:
        detail = proc.stdout.strip().replace("\n", " ")[:1600]
        raise ControllerError(
            "ACCEPTANCE_REPORT_FINALIZATION_FAILED "
            + (detail or f"rc={proc.returncode}")
        )

    pass_path = project / ".opencode-v2" / "acceptance-pass.json"
    marker = load_json(pass_path, "acceptance pass marker")
    if marker.get("protocol") != "v2-acceptance-pass-v1" or marker.get("result") != "PASS":
        raise ControllerError("acceptance finalizer produced an invalid pass marker")
    report_sha = hashlib.sha256(report.read_bytes()).hexdigest()
    pass_sha = hashlib.sha256(pass_path.read_bytes()).hexdigest()
    return {
        "kind": "terminal-acceptance-report-finalized",
        "report_sha256": report_sha,
        "acceptance_pass_sha256": pass_sha,
    }


def semantic_prompt_work_token(
    canonical_action: dict, semantic_prompt_sha256: str
) -> str:
    """Bind final acceptance idempotency to its canonical evidence packet."""
    if canonical_action != {
        "kind":"launch","agent":"acceptance-validator","mode":"final"
    }:
        return ""
    digest=str(semantic_prompt_sha256 or "")
    if not re.fullmatch(r"[0-9a-f]{64}",digest):
        raise ControllerError(
            "final acceptance semantic prompt hash is invalid"
        )
    return f"acceptance-final:{digest}"


def semantic_work_item_token(project: Path, canonical_action: dict) -> str:
    """Return stable durable work identity for sequential semantic validation."""
    if canonical_action != {
        "kind": "launch",
        "agent": "reference-researcher",
        "mode": "validation",
    }:
        return ""
    path=project/".opencode-v2"/"acceptance"/"reference-work.json"
    if not path.exists():
        return "reference-validation:initial"
    data=load_json(path,"reference work")
    status=str(data.get("status") or "")
    if status=="in_progress":
        item=str(data.get("current_item") or "")
    elif status=="complete":
        item=str(data.get("next_item") or "")
        if not item:
            item="complete"
    else:
        item=str(
            data.get("current_item")
            or data.get("next_item")
            or status
            or "initial"
        )
    return f"reference-validation:{item or 'initial'}"


def current_semantic_work_token(project: Path, canonical_action: dict) -> str:
    """Reconstruct the durable identity for the current semantic work item."""
    if canonical_action == {
        "kind":"launch","agent":"acceptance-validator","mode":"final"
    }:
        part=build_semantic_subtask(canonical_action,project)
        prompt_sha=hashlib.sha256(
            str(part.get("prompt") or "").encode("utf-8")
        ).hexdigest()
        return semantic_prompt_work_token(canonical_action,prompt_sha)
    return semantic_work_item_token(project,canonical_action)


def semantic_validation_attempt_slot(
    project: Path,
    canonical_action: dict,
) -> int | None:
    """Return the next durable validation-session ordinal for reference validation."""
    if canonical_action != {
        "kind":"launch",
        "agent":"reference-researcher",
        "mode":"validation",
    }:
        return None
    path=project/".opencode-v2"/"reference-validation-gate.json"
    if not path.exists():
        return 1
    gate=load_json(path,"reference validation gate")
    if gate.get("owner")!="supervisor" or gate.get("phase")!="validation":
        raise ControllerError("reference validation gate identity is invalid")
    try:
        attempts=int(gate.get("attempts"))
        max_attempts=int(gate.get("max_attempts"))
    except (TypeError,ValueError) as exc:
        raise ControllerError("reference validation gate counters are invalid") from exc
    if attempts < 0 or max_attempts < 1 or attempts >= max_attempts:
        raise ControllerError(
            "reference validation gate has no remaining semantic attempt slot"
        )
    if str(gate.get("state") or "")!="pending":
        raise ControllerError(
            "reference validation semantic dispatch requires a pending gate"
        )
    return attempts+1


def semantic_execution_slot(
    ledger: dict,
    state_version: str,
    root: str,
    canonical_action: dict,
    semantic_work_token: str = "",
    semantic_attempt_slot: int | None = None,
) -> tuple[int, str]:
    """Return the current semantic execution generation and its idempotency key."""
    executions = ledger.get("executions") if isinstance(ledger, dict) else {}
    if not isinstance(executions, dict):
        raise ControllerError("execution ledger executions is not an object")
    generation = 0
    while True:
        execution_id = execution_action_id(
            state_version,
            root,
            canonical_action,
            None if generation == 0 else generation,
            semantic_work_token,
            semantic_attempt_slot,
        )
        intent = executions.get(execution_id)
        if intent is None:
            return generation, execution_id
        if not isinstance(intent, dict):
            raise ControllerError(f"execution ledger entry invalid: {execution_id}")
        if (
            intent.get("action") != canonical_action
            or str(intent.get("state_version") or "") != str(state_version)
            or str(intent.get("root_session") or "") != str(root)
            or str(intent.get("semantic_work_token") or "") != str(semantic_work_token)
        ):
            raise ControllerError(f"semantic execution ledger mismatch: {execution_id}")
        recorded_slot=int(intent.get("semantic_attempt_slot") or 0)
        expected_slot=int(semantic_attempt_slot or 0)
        if recorded_slot != expected_slot:
            raise ControllerError(
                f"semantic execution attempt slot mismatch: {execution_id}"
            )
        try:
            recorded_generation = int(intent.get("semantic_generation") or 0)
        except (TypeError, ValueError) as exc:
            raise ControllerError(
                f"semantic execution generation invalid: {execution_id}"
            ) from exc
        if recorded_generation != generation:
            raise ControllerError(
                f"semantic execution generation mismatch: {execution_id}"
            )
        grant = intent.get("semantic_infrastructure_retry")
        if not grant:
            return generation, execution_id
        if not isinstance(grant, dict) or grant.get("protocol") != SEMANTIC_RETRY_PROTOCOL:
            raise ControllerError(f"semantic retry grant invalid: {execution_id}")
        try:
            next_generation = int(grant.get("next_generation"))
        except (TypeError, ValueError) as exc:
            raise ControllerError(f"semantic retry generation invalid: {execution_id}") from exc
        if next_generation != generation + 1:
            raise ControllerError(f"semantic retry generation is not contiguous: {execution_id}")
        if next_generation > MAX_SEMANTIC_INFRASTRUCTURE_RETRIES:
            raise ControllerError("semantic infrastructure retry limit exceeded")
        generation = next_generation


def blocked_reference_validation_refund_allowed(
    project: Path,
    current: dict,
    canonical_action: dict,
    intent_work_token: str,
    evidence: dict,
) -> bool:
    """Allow refund of the terminal session that itself caused a stagnant-tail block."""
    if canonical_action != {
        "kind":"launch",
        "agent":"reference-researcher",
        "mode":"validation",
    }:
        return False
    if (
        current.get("resume_phase")!="acceptance-validation"
        or current.get("actions")!=[
            {"kind":"blocked","reason":"reference-validation-blocked"}
        ]
        or semantic_work_item_token(project,canonical_action)!=intent_work_token
    ):
        return False
    path=project/".opencode-v2"/"reference-validation-gate.json"
    if not path.exists():
        return False
    try:
        gate=load_json(path,"reference validation gate")
        attempts=int(gate.get("attempts"))
        max_attempts=int(gate.get("max_attempts"))
        stagnant=int(gate.get("stagnant_tail"))
    except (ControllerError,TypeError,ValueError):
        return False
    completed=gate.get("session_ids")
    active=gate.get("active_sessions")
    sessions=evidence.get("sessions") if isinstance(evidence,dict) else None
    return bool(
        gate.get("owner")=="supervisor"
        and gate.get("phase")=="validation"
        and gate.get("state")=="blocked"
        and 1 <= attempts <= max_attempts
        and stagnant==2
        and isinstance(completed,list) and completed
        and isinstance(active,list) and not active
        and isinstance(sessions,list) and len(sessions)==1
        and completed[-1]==sessions[0]
    )


def authorize_semantic_infrastructure_retry(
    project: Path,
    base_url: str,
    execution_id: str,
    reason: str,
) -> dict:
    """Authorize one explicit retry for a proven terminal semantic child."""
    reason = str(reason or "").strip()
    if not reason:
        raise ControllerError("semantic infrastructure retry requires a reason")
    if len(reason) > 500:
        raise ControllerError("semantic infrastructure retry reason is too long")

    with execution_lock(project):
        ledger = load_execution_ledger(project)
        intent = ledger["executions"].get(execution_id)
        if not isinstance(intent, dict):
            raise ControllerError(f"unknown execution_id={execution_id}")
        action = intent.get("action") or {}
        canonical_action = canonical_execution_action(action)
        if (str(canonical_action.get("agent") or ""), str(canonical_action.get("mode") or "")) not in SEMANTIC_PROMPTS:
            raise ControllerError("infrastructure retry authorization is semantic-only")
        root = str(intent.get("root_session") or "")
        if not root:
            raise ControllerError(f"execution intent incomplete: {execution_id}")

        evidence = semantic_reconcile_evidence(
            intent, child_snapshot(project, base_url, root)
        )
        if not evidence:
            raise ControllerError(
                "semantic infrastructure retry requires native child evidence"
            )
        if semantic_child_may_still_transition(project, base_url, intent, evidence):
            raise ControllerError(
                "semantic infrastructure retry forbidden while child may still transition"
            )

        current = evaluate(project)
        intent_work_token=str(intent.get("semantic_work_token") or "")
        current_work_token=(
            current_semantic_work_token(project,canonical_action)
            if intent_work_token else ""
        )
        same_current_work=(
            current.get("state_version") == intent.get("state_version")
            and current.get("actions") == [canonical_action]
            and current_work_token == intent_work_token
        )
        blocked_gate_recovery=blocked_reference_validation_refund_allowed(
            project,current,canonical_action,intent_work_token,evidence
        )
        if not (same_current_work or blocked_gate_recovery):
            raise ControllerError(
                "semantic infrastructure retry requires the same current deterministic work item"
            )

        try:
            generation = int(intent.get("semantic_generation") or 0)
        except (TypeError, ValueError) as exc:
            raise ControllerError("semantic execution generation is invalid") from exc
        if generation >= MAX_SEMANTIC_INFRASTRUCTURE_RETRIES:
            raise ControllerError("semantic infrastructure retry limit reached")

        existing_grant = intent.get("semantic_infrastructure_retry")
        if existing_grant:
            if (
                isinstance(existing_grant, dict)
                and existing_grant.get("protocol") == SEMANTIC_RETRY_PROTOCOL
                and existing_grant.get("reason") == reason
            ):
                return {
                    "protocol": SEMANTIC_RETRY_PROTOCOL,
                    "execution_id": execution_id,
                    "next_generation": existing_grant.get("next_generation"),
                    "idempotent": True,
                }
            raise ControllerError("semantic infrastructure retry already authorized")

        next_generation = generation + 1
        intent_attempt_slot=(
            int(intent.get("semantic_attempt_slot"))
            if intent.get("semantic_attempt_slot") is not None else None
        )
        next_id = execution_action_id(
            str(intent["state_version"]),
            root,
            canonical_action,
            next_generation,
            intent_work_token,
            intent_attempt_slot,
        )
        if next_id in ledger["executions"]:
            raise ControllerError("next semantic retry execution already exists")

        intent["semantic_infrastructure_retry"] = {
            "protocol": SEMANTIC_RETRY_PROTOCOL,
            "source": "operator-controller",
            "reason": reason,
            "authorized_at_ms": int(time.time() * 1000),
            "child_sessions": list(evidence.get("sessions") or []),
            "next_generation": next_generation,
            "blocked_gate_recovery":bool(blocked_gate_recovery),
        }
        save_execution_ledger(project, ledger)
        return {
            "protocol": SEMANTIC_RETRY_PROTOCOL,
            "execution_id": execution_id,
            "next_generation": next_generation,
            "next_execution_id": next_id,
            "idempotent": False,
        }


def final_tests_reconcile_evidence(project: Path, intent: dict) -> dict | None:
    """Return evidence only for the exact report recorded by this executor."""
    expected = str(intent.get("test_report_sha256") or "")
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        return None
    path = project / ".opencode-v2" / "TEST_REPORT.json"
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        report = load_json(path, "final test report")
    except ControllerError:
        return None
    if actual != expected or report.get("status") not in {"pass", "fail"}:
        return None
    return {
        "kind": "final-test-report",
        "status": report["status"],
        "test_report_sha256": actual,
    }


def execute_first_planner(
    project: Path,
    base_url: str,
    result: dict,
    explicit_root: str = "",
) -> dict:
    actions = result["actions"]
    planner_actions = [
        action for action in actions
        if isinstance(action, dict)
        and action.get("kind") == "launch"
        and action.get("agent") == "implementation-planner"
    ]
    if len(planner_actions) != 1:
        raise ControllerError(
            "planner executor requires exactly one deterministic planner launch: "
            + json.dumps(planner_actions, sort_keys=True)
        )
    launch = planner_actions[0]
    canonical_action = canonical_execution_action(launch)
    unsupported = [
        action for action in actions
        if not (
            action == launch
            or (isinstance(action, dict) and action.get("kind") in {"wait", "rescan"})
        )
    ]
    if unsupported:
        raise ControllerError(
            "mixed/unsupported deterministic actions in planner-only slice: "
            + json.dumps(unsupported, sort_keys=True)
        )
    part = build_planner_subtask(canonical_action)
    root = resolve_root_session(project, base_url, explicit_root)

    with execution_lock(project):
        current = evaluate(project)
        if current["state_version"] != result["state_version"]:
            raise ControllerError(
                f"state changed before dispatch: selected={result['state_version']} "
                f"current={current['state_version']}"
            )
        if current["actions"] != result["actions"]:
            raise ControllerError("deterministic actions changed before dispatch")
        dispatch_generation = planner_dispatch_generation(project)
        execution_id = execution_action_id(
            result["state_version"], root, canonical_action, dispatch_generation
        )
        ledger = load_execution_ledger(project)
        executions = ledger["executions"]
        existing = executions.get(execution_id)
        if existing is not None:
            if not isinstance(existing, dict) or existing.get("action") != canonical_action:
                raise ControllerError(f"execution ledger action mismatch: {execution_id}")
            evidence = planner_reconcile_evidence(
                existing, child_snapshot(project, base_url, root)
            )
            if evidence:
                return replay_receipt(existing, evidence)
            raise ControllerError(
                "AMBIGUOUS_EXECUTION planner launch has no child evidence; "
                f"execution_id={execution_id} replay remains forbidden"
            )

        require_current_runtime_contract(project, base_url)
        ensure_root_idle(project, base_url, root)
        baseline_children = child_snapshot(project, base_url, root)
        intent = {
            "execution_id": execution_id,
            "state_version": result["state_version"],
            "root_session": root,
            "action": canonical_action,
            "dispatch_generation": dispatch_generation,
            "transport": "prompt_async+SubtaskPart",
            "transport_may_have_been_attempted": True,
            "created_at_ms": int(time.time() * 1000),
            "baseline_child_ids": sorted(
                str(item.get("id")) for item in baseline_children
                if isinstance(item, dict) and item.get("id")
            ),
        }
        executions[execution_id] = intent
        save_execution_ledger(project, ledger)
        url = workspace_url(
            base_url, f"/session/{urllib.parse.quote(root)}/prompt_async", project
        )
        try:
            status, body = http_json("POST", url, {
                "agent": "transport-root",
                "model": {"providerID": "v2noop", "modelID": "root-noop"},
                "parts": [part],
            }, timeout=10.0)
        except ControllerError as exc:
            raise ControllerError(
                f"{exc}; execution_id={execution_id}; transport outcome is ambiguous; "
                "blind replay is forbidden; use --reconcile-execution with this execution_id"
            ) from exc
        if status != 204:
            raise ControllerError(
                f"prompt_async returned unexpected HTTP {status}: {body!r}; "
                f"execution_id={execution_id} remains ambiguous and cannot be replayed blindly"
            )
        return {
            "protocol": EXECUTION_RECEIPT_PROTOCOL,
            "state_version": result["state_version"],
            "root_session": root,
            "action": canonical_action,
            "execution_id": execution_id,
            "dispatch_generation": dispatch_generation,
            "http_status": status,
            "transport": "prompt_async+SubtaskPart",
            "idempotency_intent_persisted": True,
            "replay_suppressed": False,
        }


def execute_first_semantic(
    project: Path,
    base_url: str,
    result: dict,
    explicit_root: str = "",
) -> dict:
    """Dispatch exactly one non-worker semantic phase through the technical root."""
    actions = result["actions"]
    selected = [
        action for action in actions
        if isinstance(action, dict)
        and (str(action.get("agent") or ""), str(action.get("mode") or ""))
        in SEMANTIC_PROMPTS
    ]
    if len(selected) != 1:
        raise ControllerError(
            "semantic executor requires exactly one deterministic semantic launch: "
            + json.dumps(selected, sort_keys=True)
        )
    launch = selected[0]
    canonical_action = canonical_execution_action(launch)
    unsupported = [
        action for action in actions
        if not (
            action == launch
            or (isinstance(action, dict) and action.get("kind") in {"wait", "rescan"})
        )
    ]
    if unsupported:
        raise ControllerError(
            "mixed/unsupported deterministic actions in semantic-only slice: "
            + json.dumps(unsupported, sort_keys=True)
        )
    part = build_semantic_subtask(canonical_action, project)
    semantic_prompt_sha256 = hashlib.sha256(
        str(part.get("prompt") or "").encode("utf-8")
    ).hexdigest()
    root = resolve_root_session(project, base_url, explicit_root)

    with execution_lock(project):
        current = evaluate(project)
        if current["state_version"] != result["state_version"]:
            raise ControllerError(
                f"state changed before dispatch: selected={result['state_version']} "
                f"current={current['state_version']}"
            )
        if current["actions"] != result["actions"]:
            raise ControllerError("deterministic actions changed before dispatch")
        ledger = load_execution_ledger(project)
        semantic_work_token = (
            semantic_prompt_work_token(
                canonical_action,semantic_prompt_sha256
            )
            or semantic_work_item_token(project, canonical_action)
        )
        semantic_attempt_slot = semantic_validation_attempt_slot(
            project, canonical_action
        )
        semantic_generation, execution_id = semantic_execution_slot(
            ledger,
            result["state_version"],
            root,
            canonical_action,
            semantic_work_token,
            semantic_attempt_slot,
        )
        executions = ledger["executions"]
        existing = executions.get(execution_id)
        if existing is not None:
            if not isinstance(existing, dict) or existing.get("action") != canonical_action:
                raise ControllerError(f"execution ledger action mismatch: {execution_id}")
            if str(existing.get("semantic_prompt_sha256") or "") != semantic_prompt_sha256:
                raise ControllerError(
                    "semantic prompt changed without deterministic state transition: "
                    f"execution_id={execution_id}"
                )
            evidence = semantic_reconcile_evidence(
                existing, child_snapshot(project, base_url, root)
            )
            if evidence:
                if not semantic_child_may_still_transition(
                    project, base_url, existing, evidence
                ):
                    finalized = finalize_terminal_acceptance_report(
                        project, base_url, existing, evidence
                    )
                    if finalized:
                        existing["terminal_acceptance_finalization"] = {
                            **finalized,
                            "finalized_at_ms": int(time.time() * 1000),
                        }
                        save_execution_ledger(project, ledger)
                        return replay_receipt(existing, finalized)
                    raise ControllerError(
                        "SEMANTIC_CHILD_TERMINATED_WITHOUT_STATE_TRANSITION; "
                        f"execution_id={execution_id}; inspect durable guard errors "
                        "before issuing a new deterministic action"
                    )
                return replay_receipt(existing, evidence)
            raise ControllerError(
                "AMBIGUOUS_EXECUTION semantic launch has no child evidence; "
                f"execution_id={execution_id} replay remains forbidden"
            )

        require_current_runtime_contract(project, base_url)
        ensure_root_idle(project, base_url, root)
        baseline_children = child_snapshot(project, base_url, root)
        intent = {
            "execution_id": execution_id,
            "state_version": result["state_version"],
            "root_session": root,
            "action": canonical_action,
            "semantic_generation": semantic_generation,
            "semantic_work_token": semantic_work_token,
            "semantic_attempt_slot": semantic_attempt_slot,
            "semantic_prompt_sha256": semantic_prompt_sha256,
            "transport": "prompt_async+SubtaskPart",
            "transport_may_have_been_attempted": True,
            "created_at_ms": int(time.time() * 1000),
            "baseline_child_ids": sorted(
                str(item.get("id")) for item in baseline_children
                if isinstance(item, dict) and item.get("id")
            ),
        }
        executions[execution_id] = intent
        save_execution_ledger(project, ledger)
        url = workspace_url(
            base_url, f"/session/{urllib.parse.quote(root)}/prompt_async", project
        )
        try:
            status, body = http_json("POST", url, {
                "agent": "transport-root",
                "model": {"providerID": "v2noop", "modelID": "root-noop"},
                "parts": [part],
            }, timeout=10.0)
        except ControllerError as exc:
            raise ControllerError(
                f"{exc}; execution_id={execution_id}; transport outcome is ambiguous; "
                "blind replay is forbidden; use --reconcile-execution with this execution_id"
            ) from exc
        if status != 204:
            raise ControllerError(
                f"prompt_async returned unexpected HTTP {status}: {body!r}; "
                f"execution_id={execution_id} remains ambiguous and cannot be replayed blindly"
            )
        return {
            "protocol": EXECUTION_RECEIPT_PROTOCOL,
            "state_version": result["state_version"],
            "root_session": root,
            "action": canonical_action,
            "execution_id": execution_id,
            "http_status": status,
            "transport": "prompt_async+SubtaskPart",
            "idempotency_intent_persisted": True,
            "replay_suppressed": False,
        }


def execute_first_final_tests(project: Path, result: dict) -> dict:
    """Run the selected final-test action with the repository-owned harness.

    The project-local wrapper is deliberately not trusted here: an implementation
    worker can edit project files, while this controller must execute the known
    harness that created the protocol.  The intent is durable before subprocess
    execution; an interrupted/unknown invocation therefore cannot be replayed.
    """
    actions = result["actions"]
    selected = [
        action for action in actions
        if isinstance(action, dict) and action.get("kind") == "run_final_tests"
    ]
    if len(selected) != 1:
        raise ControllerError(
            "final-tests executor requires exactly one deterministic final-test action: "
            + json.dumps(selected, sort_keys=True)
        )
    launch = selected[0]
    canonical_action = canonical_execution_action(launch)
    unsupported = [
        action for action in actions
        if not (
            action == launch
            or (isinstance(action, dict) and action.get("kind") in {"wait", "rescan"})
        )
    ]
    if unsupported:
        raise ControllerError(
            "mixed/unsupported deterministic actions in final-tests slice: "
            + json.dumps(unsupported, sort_keys=True)
        )
    execution_id = execution_action_id(
        result["state_version"], "deterministic-final-tests", canonical_action
    )
    runner = HARNESS_ROOT / "scripts" / "run-checks.py"
    if not runner.is_file():
        raise ControllerError(f"trusted final-test harness is missing: {runner}")

    with execution_lock(project):
        current = evaluate(project)
        if current["state_version"] != result["state_version"]:
            raise ControllerError(
                f"state changed before final tests: selected={result['state_version']} "
                f"current={current['state_version']}"
            )
        if current["actions"] != result["actions"]:
            raise ControllerError("deterministic actions changed before final tests")
        ledger = load_execution_ledger(project)
        executions = ledger["executions"]
        existing = executions.get(execution_id)
        if existing is not None:
            if not isinstance(existing, dict) or existing.get("action") != canonical_action:
                raise ControllerError(f"execution ledger action mismatch: {execution_id}")
            evidence = final_tests_reconcile_evidence(project, existing)
            if evidence:
                return replay_receipt(existing, evidence)
            raise ControllerError(
                "AMBIGUOUS_EXECUTION final-test invocation has no matching durable "
                f"report; execution_id={execution_id} cannot be replayed blindly"
            )

        intent = {
            "execution_id": execution_id,
            "state_version": result["state_version"],
            "root_session": "deterministic-final-tests",
            "action": canonical_action,
            "transport": "trusted-run-checks",
            "transport_may_have_been_attempted": True,
            "created_at_ms": int(time.time() * 1000),
        }
        executions[execution_id] = intent
        save_execution_ledger(project, ledger)
        completed = subprocess.run(
            [sys.executable, str(runner), "--project", str(project)],
            cwd=project,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        path = project / ".opencode-v2" / "TEST_REPORT.json"
        try:
            report = load_json(path, "final test report")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except ControllerError as exc:
            raise ControllerError(
                f"final-test harness exited {completed.returncode} without a durable report; "
                f"execution_id={execution_id} remains ambiguous and cannot be replayed blindly"
            ) from exc
        if report.get("status") not in {"pass", "fail"}:
            raise ControllerError(
                f"final-test report has invalid status: {report.get('status')!r}; "
                f"execution_id={execution_id} remains ambiguous"
            )
        if (completed.returncode == 0) != (report["status"] == "pass"):
            raise ControllerError(
                "final-test process/result mismatch; "
                f"execution_id={execution_id} remains ambiguous"
            )
        intent["test_report_sha256"] = digest
        intent["returncode"] = completed.returncode
        save_execution_ledger(project, ledger)
        return {
            "protocol": EXECUTION_RECEIPT_PROTOCOL,
            "state_version": result["state_version"],
            "root_session": "deterministic-final-tests",
            "action": canonical_action,
            "execution_id": execution_id,
            "transport": "trusted-run-checks",
            "returncode": completed.returncode,
            "test_status": report["status"],
            "test_report_sha256": digest,
            "idempotency_intent_persisted": True,
            "replay_suppressed": False,
        }


def unsupported_task_splitter_actions(actions: list, selected: dict) -> list:
    """Return actions outside the intentionally narrow splitter executor slice."""
    unsupported = []
    for action in actions:
        if not isinstance(action, dict):
            unsupported.append(action)
            continue
        if action.get("kind") in {"wait", "rescan"}:
            continue
        if action.get("kind") != "launch" or not action.get("deliverable"):
            unsupported.append(action)
            continue
        if action.get("agent") != "task-splitter":
            continue
        try:
            is_selected = canonical_execution_action(action) == selected
        except ControllerError:
            is_selected = False
        if not is_selected:
            unsupported.append(action)
    return unsupported


def execute_first_implementation(
    project: Path,
    base_url: str,
    result: dict,
    explicit_root: str = "",
) -> dict:
    actions = result["actions"]
    launch = next(
        (
            action
            for action in actions
            if isinstance(action, dict)
            and action.get("kind") == "launch"
            and action.get("deliverable")
            and action.get("agent") != "task-splitter"
        ),
        None,
    )
    if launch is None:
        raise ControllerError(
            "current deterministic action set has no implementation launch; "
            "this executor slice intentionally fails closed"
        )

    def is_implementation_launch(action: object) -> bool:
        return (
            isinstance(action, dict)
            and action.get("kind") == "launch"
            and bool(action.get("deliverable"))
            and action.get("agent") != "task-splitter"
        )

    unsupported = [
        action
        for action in actions
        if not (
            is_implementation_launch(action)
            or (
                isinstance(action, dict)
                and action.get("kind") in {"wait", "rescan"}
            )
        )
    ]
    if unsupported:
        raise ControllerError(
            "mixed/unsupported deterministic actions in implementation-only slice: "
            + json.dumps(unsupported, sort_keys=True)
        )

    agent = str(launch["agent"])
    did = str(launch["deliverable"])
    prompt = canonical_implementation_prompt(project, agent, did)
    part = build_implementation_subtask(launch, prompt)

    root = resolve_root_session(project, base_url, explicit_root)
    canonical_action = canonical_execution_action(launch)

    with execution_lock(project):
        current = evaluate(project)
        if current["state_version"] != result["state_version"]:
            raise ControllerError(
                f"state changed before dispatch: selected={result['state_version']} "
                f"current={current['state_version']}"
            )
        if current["actions"] != result["actions"]:
            raise ControllerError("deterministic actions changed before dispatch")

        dispatch_generation = attempt_failure_generation(project, did)
        execution_id = execution_action_id(
            result["state_version"], root, canonical_action, dispatch_generation
        )

        ledger = load_execution_ledger(project)
        executions = ledger["executions"]
        existing = executions.get(execution_id)
        same_id_replay_history=[]
        same_id_superseded_history=[]
        if existing is not None:
            if not isinstance(existing, dict):
                raise ControllerError(f"execution ledger entry invalid: {execution_id}")
            if existing.get("action") != canonical_action:
                raise ControllerError(f"execution ledger action mismatch: {execution_id}")
            attempts = attempt_snapshot(project, did)
            children = child_snapshot(project, base_url, root)
            orphans=replayable_zero_work_orphans(
                project,base_url,result,existing,attempts,children,did
            )
            superseded=replayable_superseded_orphans(
                project,base_url,result,existing,children,did
            )
            if orphans or superseded:
                now_ms=int(time.time()*1000)
                if orphans:
                    existing.setdefault("zero_work_orphan_replays",[]).append({
                        "deliverable":did,
                        "sessions":orphans,
                        "observed_at_ms":now_ms,
                        "reason":"same-execution-reusable-reservation-terminal-zero-work-child",
                    })
                    same_id_replay_history=list(
                        existing.get("zero_work_orphan_replays") or []
                    )
                if superseded:
                    existing.setdefault("superseded_child_replays",[]).append({
                        "deliverable":did,
                        "sessions":superseded,
                        "observed_at_ms":now_ms,
                        "reason":"same-execution-reusable-reservation-supervisor-superseded-child",
                    })
                    same_id_superseded_history=list(
                        existing.get("superseded_child_replays") or []
                    )
                save_execution_ledger(project,ledger)
            else:
                attempts = bind_unbound_native_child(
                    project, base_url, existing, did, agent, attempts, children
                )
                evidence = reconcile_execution_evidence(existing, attempts, children)
                if evidence:
                    return replay_receipt(existing, evidence)
                raise ControllerError(
                    "AMBIGUOUS_EXECUTION replay forbidden: "
                    f"execution_id={execution_id} state_version={result['state_version']} "
                    f"deliverable={did} no preclaim/child evidence observed"
                )

        current_attempt = attempt_snapshot(project, did)
        prior_intents = prior_logical_implementation_intents(
            executions,
            execution_id,
            root,
            canonical_action,
            dispatch_generation,
            int(current_attempt.get("count") or 0),
        )
        if current_attempt_is_terminal(project,did):
            current_count=int(current_attempt.get("count") or 0)
            prior_intents=[
                intent for intent in prior_intents
                if int(
                    (intent.get("baseline_attempt") or {}).get("count") or 0
                )==current_count
            ]
        if prior_intents:
            attempts = current_attempt
            children = child_snapshot(project, base_url, root)
            safe_replays=[]
            for prior in prior_intents:
                orphans=replayable_zero_work_orphans(
                    project,base_url,result,prior,attempts,children,did
                )
                superseded=replayable_superseded_orphans(
                    project,base_url,result,prior,children,did
                )
                if orphans or superseded:
                    evidence=reconcile_execution_evidence(
                        prior,attempts,children
                    )
                    if (
                        isinstance(evidence,dict)
                        and evidence.get("kind")=="preclaim-reservation"
                    ):
                        safe_replays.append((prior,orphans,superseded))
                        continue
                attempts = bind_unbound_native_child(
                    project, base_url, prior, did, agent, attempts, children
                )
                evidence = reconcile_execution_evidence(
                    prior, attempts, children
                )
                if evidence:
                    return replay_receipt(prior, evidence)
                raise ControllerError(
                    "LOGICAL_IMPLEMENTATION_DISPATCH_SETTLING "
                    f"deliverable={did} generation={dispatch_generation} "
                    f"prior_execution_id={prior.get('execution_id','')}"
                )
            if not safe_replays:
                raise ControllerError(
                    "LOGICAL_IMPLEMENTATION_DISPATCH_SETTLING "
                    f"deliverable={did} generation={dispatch_generation}"
                )
            now_ms=int(time.time()*1000)
            for prior,orphans,superseded in safe_replays:
                if orphans:
                    prior.setdefault("zero_work_orphan_replays",[]).append({
                        "deliverable":did,
                        "sessions":orphans,
                        "observed_at_ms":now_ms,
                        "reason":"reusable-reservation-terminal-zero-work-child",
                    })
                if superseded:
                    prior.setdefault("superseded_child_replays",[]).append({
                        "deliverable":did,
                        "sessions":superseded,
                        "observed_at_ms":now_ms,
                        "reason":"reusable-reservation-supervisor-superseded-child",
                    })
            save_execution_ledger(project,ledger)

        require_current_runtime_contract(project, base_url)
        ensure_root_idle(project, base_url, root)
        baseline_attempt = attempt_snapshot(project, did)
        baseline_children = child_snapshot(project, base_url, root)
        intent = {
            "execution_id": execution_id,
            "state_version": result["state_version"],
            "root_session": root,
            "action": canonical_action,
            "dispatch_generation": dispatch_generation,
            "transport": "prompt_async+SubtaskPart",
            "transport_may_have_been_attempted": True,
            "created_at_ms": int(time.time() * 1000),
            "baseline_attempt": baseline_attempt,
            "baseline_child_ids": sorted(
                str(item.get("id"))
                for item in baseline_children
                if isinstance(item, dict) and item.get("id")
            ),
        }
        if same_id_replay_history:
            intent["zero_work_orphan_replays"]=same_id_replay_history
        if same_id_superseded_history:
            intent["superseded_child_replays"]=same_id_superseded_history
        executions[execution_id] = intent
        save_execution_ledger(project, ledger)

        url = workspace_url(
            base_url,
            f"/session/{urllib.parse.quote(root)}/prompt_async",
            project,
        )
        try:
            payload = {
                "agent": "transport-root",
                "model": {"providerID": "v2noop", "modelID": "root-noop"},
                "parts": [part],
            }
            status, body = http_json("POST", url, payload, timeout=10.0)
        except ControllerError as exc:
            raise ControllerError(
                f"{exc}; execution_id={execution_id}; transport outcome is ambiguous; "
                "blind replay is forbidden; use --reconcile-execution with this execution_id"
            ) from exc
        if status != 204:
            raise ControllerError(
                f"prompt_async returned unexpected HTTP {status}: {body!r}; "
                f"execution_id={execution_id} remains ambiguous and cannot be replayed blindly"
            )

        return {
            "protocol": EXECUTION_RECEIPT_PROTOCOL,
            "state_version": result["state_version"],
            "root_session": root,
            "action": canonical_action,
            "execution_id": execution_id,
            "http_status": status,
            "transport": "prompt_async+SubtaskPart",
            "idempotency_intent_persisted": True,
            "replay_suppressed": False,
        }


def execute_first_task_splitter(
    project: Path,
    base_url: str,
    result: dict,
    explicit_root: str = "",
) -> dict:
    actions = result["actions"]
    split_actions = [
        action
        for action in actions
        if isinstance(action, dict)
        and action.get("kind") == "launch"
        and action.get("agent") == "task-splitter"
        and action.get("deliverable")
    ]
    if len(split_actions) != 1:
        raise ControllerError(
            "task-splitter executor requires exactly one deterministic splitter launch: "
            + json.dumps(split_actions, sort_keys=True)
        )
    launch = split_actions[0]
    canonical_action = canonical_execution_action(launch)
    unsupported = unsupported_task_splitter_actions(actions, canonical_action)
    if unsupported:
        raise ControllerError(
            "mixed/unsupported deterministic actions in task-splitter slice: "
            + json.dumps(unsupported, sort_keys=True)
        )

    root = resolve_root_session(project, base_url, explicit_root)
    execution_id = execution_action_id(result["state_version"], root, canonical_action)
    did = str(canonical_action["deliverable"])

    with execution_lock(project):
        current = evaluate(project)
        if current["state_version"] != result["state_version"]:
            raise ControllerError(
                f"state changed before dispatch: selected={result['state_version']} "
                f"current={current['state_version']}"
            )
        if current["actions"] != result["actions"]:
            raise ControllerError("deterministic actions changed before dispatch")

        ledger = load_execution_ledger(project)
        executions = ledger["executions"]
        existing = executions.get(execution_id)
        if existing is not None:
            if not isinstance(existing, dict):
                raise ControllerError(f"execution ledger entry invalid: {execution_id}")
            if existing.get("action") != canonical_action:
                raise ControllerError(f"execution ledger action mismatch: {execution_id}")
            attempts = attempt_snapshot(project, did)
            children = child_snapshot(project, base_url, root)
            # Splitters own a durable lease/proposal lifecycle, not a worker
            # attempt reservation. A native splitter child is therefore replay
            # evidence by itself and must never enter implementation binding.
            evidence = reconcile_execution_evidence(existing, attempts, children)
            if evidence:
                return replay_receipt(existing, evidence)
            raise ControllerError(
                "AMBIGUOUS_EXECUTION replay forbidden: "
                f"execution_id={execution_id} state_version={result['state_version']} "
                f"deliverable={did} no child evidence observed"
            )

        prior_intents=prior_logical_task_splitter_intents(
            executions,execution_id,root,canonical_action
        )
        if prior_intents:
            attempts=attempt_snapshot(project,did)
            children=child_snapshot(project,base_url,root)
            for prior in prior_intents:
                evidence=reconcile_execution_evidence(prior,attempts,children)
                if evidence:
                    return replay_receipt(prior,evidence)
            raise ControllerError(
                "LOGICAL_TASK_SPLITTER_DISPATCH_SETTLING "
                f"deliverable={did} generation={canonical_action.get('generation')}"
            )

        require_current_runtime_contract(project, base_url)
        ensure_root_idle(project, base_url, root)
        request_path = project / ".opencode-v2" / "work" / f"{did}.split-request.json"
        part = build_task_splitter_subtask(
            canonical_action, load_json(request_path, f"canonical split request {did}")
        )
        baseline_attempt = attempt_snapshot(project, did)
        baseline_children = child_snapshot(project, base_url, root)
        intent = {
            "execution_id": execution_id,
            "state_version": result["state_version"],
            "root_session": root,
            "action": canonical_action,
            "transport": "prompt_async+SubtaskPart",
            "transport_may_have_been_attempted": True,
            "created_at_ms": int(time.time() * 1000),
            "baseline_attempt": baseline_attempt,
            "baseline_child_ids": sorted(
                str(item.get("id"))
                for item in baseline_children
                if isinstance(item, dict) and item.get("id")
            ),
        }
        executions[execution_id] = intent
        save_execution_ledger(project, ledger)

        url = workspace_url(
            base_url,
            f"/session/{urllib.parse.quote(root)}/prompt_async",
            project,
        )
        try:
            payload = {
                "agent": "transport-root",
                "model": {"providerID": "v2noop", "modelID": "root-noop"},
                "parts": [part],
            }
            status, body = http_json("POST", url, payload, timeout=10.0)
        except ControllerError as exc:
            raise ControllerError(
                f"{exc}; execution_id={execution_id}; transport outcome is ambiguous; "
                "blind replay is forbidden; use --reconcile-execution with this execution_id"
            ) from exc
        if status != 204:
            raise ControllerError(
                f"prompt_async returned unexpected HTTP {status}: {body!r}; "
                f"execution_id={execution_id} remains ambiguous and cannot be replayed blindly"
            )

        return {
            "protocol": EXECUTION_RECEIPT_PROTOCOL,
            "state_version": result["state_version"],
            "root_session": root,
            "action": canonical_action,
            "execution_id": execution_id,
            "http_status": status,
            "transport": "prompt_async+SubtaskPart",
            "idempotency_intent_persisted": True,
            "replay_suppressed": False,
        }


def reconcile_execution(project: Path, base_url: str, execution_id: str) -> dict:
    with execution_lock(project):
        ledger = load_execution_ledger(project)
        intent = ledger["executions"].get(execution_id)
        if not isinstance(intent, dict):
            raise ControllerError(f"unknown execution_id={execution_id}")
        action = intent.get("action") or {}
        did = str(action.get("deliverable") or "")
        root = str(intent.get("root_session") or "")
        agent = str(action.get("agent") or "")
        if action.get("kind") == "run_final_tests":
            evidence = final_tests_reconcile_evidence(project, intent)
            if not evidence:
                raise ControllerError(
                    "AMBIGUOUS_EXECUTION no matching final-test report observed; "
                    f"execution_id={execution_id} replay remains forbidden"
                )
            return replay_receipt(intent, evidence)
        if not root:
            raise ControllerError(f"execution intent incomplete: {execution_id}")
        if agent == "implementation-planner":
            evidence = planner_reconcile_evidence(
                intent, child_snapshot(project, base_url, root)
            )
            if not evidence:
                raise ControllerError(
                    "AMBIGUOUS_EXECUTION no planner child evidence observed; "
                    f"execution_id={execution_id} replay remains forbidden"
                )
            return replay_receipt(intent, evidence)
        if (agent, str(action.get("mode") or "")) in SEMANTIC_PROMPTS:
            evidence = semantic_reconcile_evidence(
                intent, child_snapshot(project, base_url, root)
            )
            if not evidence:
                raise ControllerError(
                    "AMBIGUOUS_EXECUTION no semantic child evidence observed; "
                    f"execution_id={execution_id} replay remains forbidden"
                )
            return replay_receipt(intent, evidence)
        if agent == "task-splitter":
            if not did:
                raise ControllerError(f"execution intent incomplete: {execution_id}")
            evidence = reconcile_execution_evidence(
                intent, attempt_snapshot(project, did), child_snapshot(project, base_url, root)
            )
            if not evidence:
                raise ControllerError(
                    "AMBIGUOUS_EXECUTION no splitter child evidence observed; "
                    f"execution_id={execution_id} replay remains forbidden"
                )
            return replay_receipt(intent, evidence)
        if not did:
            raise ControllerError(f"execution intent incomplete: {execution_id}")
        attempts = attempt_snapshot(project, did)
        children = child_snapshot(project, base_url, root)
        attempts = bind_unbound_native_child(
            project, base_url, intent, did, agent, attempts, children
        )
        evidence = reconcile_execution_evidence(intent, attempts, children)
        if not evidence:
            raise ControllerError(
                "AMBIGUOUS_EXECUTION no preclaim/child evidence observed; "
                f"execution_id={execution_id} replay remains forbidden"
            )
        return replay_receipt(intent, evidence)


def selftest() -> None:
    cases = [
        (
            {
                "state_version": "a",
                "resume_phase": "execution",
                "scheduler": {"active_workers": 0, "available_worker_slots": 2},
                "eligible": ["D001", "D002"],
                "eligible_roles": {"D001": "probe-builder", "D002": "feature-builder"},
                "execution_blockers": [],
            },
            [
                {"kind": "launch", "agent": "probe-builder", "deliverable": "D001"},
                {"kind": "launch", "agent": "feature-builder", "deliverable": "D002"},
            ],
        ),
        (
            {
                "state_version": "b",
                "resume_phase": "execution",
                "scheduler": {"active_workers": 1, "available_worker_slots": 0},
                "eligible": [],
                "eligible_roles": {},
                "execution_blockers": [],
            },
            [{"kind": "wait"}],
        ),
        (
            {
                "state_version": "c",
                "resume_phase": "implementation-plan",
                "plan": {"next_action": "repair", "blocked": False},
                "scheduler": {},
                "eligible": [],
                "execution_blockers": [],
            },
            [{"kind": "launch", "agent": "implementation-planner", "mode": "repair"}],
        ),
        (
            {
                "state_version": "d",
                "resume_phase": "complete",
                "scheduler": {},
                "eligible": [],
                "execution_blockers": [],
            },
            [{"kind": "complete", "result": "ACCEPTANCE_PASS"}],
        ),
    ]
    for idx, (decision, expected) in enumerate(cases, 1):
        actual = select_actions(decision)
        if actual != expected:
            raise ControllerError(
                f"selftest case {idx} mismatch: actual={actual!r} expected={expected!r}"
            )

    child_intent = {
        "root_session": "root-a",
        "action": {"agent": "feature-builder", "deliverable": "D042"},
        "baseline_child_ids": ["old-child"],
    }
    child_rows = [
        {"id": "old-child", "parentID": "root-a", "agent": "feature-builder"},
        {"id": "new-child", "parentID": "root-a", "agent": "feature-builder"},
        {"id": "wrong-agent", "parentID": "root-a", "agent": "probe-builder"},
        {"id": "wrong-parent", "parentID": "root-b", "agent": "feature-builder"},
    ]
    if native_child_ids(child_intent, child_rows) != ["new-child"]:
        raise ControllerError("native-child filtering selftest failed")

    payload = build_implementation_subtask(
        {"kind": "launch", "agent": "probe-builder", "deliverable": "D042"},
        "DELIVERABLE: D042\ncanonical",
    )
    expected_payload = {
        "type": "subtask",
        "prompt": "DELIVERABLE: D042\ncanonical",
        "description": "Execute D042",
        "agent": "probe-builder",
    }
    if payload != expected_payload:
        raise ControllerError(
            f"subtask payload mismatch actual={payload!r} expected={expected_payload!r}"
        )

    split_action = {
        "kind": "launch", "agent": "task-splitter", "deliverable": "D042", "generation": 3,
    }
    split_request = {"parent_id":"D042","generation":3,"depth":0}
    split_payload = build_task_splitter_subtask(split_action, split_request)
    if split_payload != {
        "type": "subtask",
        "prompt": (
            "SPLIT_PARENT: D042\nCANONICAL_SPLIT_REQUEST_JSON_BEGIN\n"
            "{\"depth\":0,\"generation\":3,\"parent_id\":\"D042\"}\n"
            "CANONICAL_SPLIT_REQUEST_JSON_END"
        ),
        "description": "Split D042", "agent": "task-splitter",
    }:
        raise ControllerError(f"task-splitter payload mismatch actual={split_payload!r}")
    if canonical_execution_action(split_action).get("generation") != 3:
        raise ControllerError("task-splitter execution action did not bind generation")
    planner_action = {
        "kind": "launch", "agent": "implementation-planner", "mode": "repair",
    }
    if canonical_execution_action(planner_action) != planner_action:
        raise ControllerError("planner execution action did not retain repair mode")
    fresh_planner_action = {
        "kind": "launch", "agent": "implementation-planner", "mode": "fresh",
    }
    if canonical_execution_action(fresh_planner_action) != fresh_planner_action:
        raise ControllerError("planner execution action did not retain fresh mode")
    planner_payload = build_planner_subtask(planner_action)
    if planner_payload["agent"] != "implementation-planner" or not planner_payload[
        "prompt"
    ].startswith("Repair structured implementation planning for this project."):
        raise ControllerError("planner subtask payload mismatch")
    semantic_action = {
        "kind": "launch", "agent": "acceptance-planner", "mode": "fresh",
    }
    if canonical_execution_action(semantic_action) != semantic_action:
        raise ControllerError("semantic execution action did not retain fresh mode")
    semantic_payload = build_semantic_subtask(semantic_action)
    if semantic_payload["agent"] != "acceptance-planner" or "command" in semantic_payload:
        raise ControllerError("semantic subtask payload mismatch")
    final_tests_action = {
        "kind": "run_final_tests", "command": ".opencode-v2/bin/run-checks",
    }
    if canonical_execution_action(final_tests_action) != final_tests_action:
        raise ControllerError("final-tests execution action was not canonical")
    try:
        canonical_execution_action({"kind": "run_final_tests", "command": "true"})
    except ControllerError:
        pass
    else:
        raise ControllerError("unsafe final-tests command was accepted")
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        ctrl = project / ".opencode-v2"
        ctrl.mkdir()
        report = ctrl / "TEST_REPORT.json"
        report.write_text('{"status":"pass"}\n', encoding="utf-8")
        digest = hashlib.sha256(report.read_bytes()).hexdigest()
        evidence = final_tests_reconcile_evidence(project, {
            "test_report_sha256": digest,
        })
        if not evidence or evidence.get("status") != "pass":
            raise ControllerError("final-tests report reconciliation mismatch")
    if semantic_child_may_still_transition.__name__ != "semantic_child_may_still_transition":
        raise ControllerError("semantic transition guard is unavailable")
    for action in (
        {"kind": "launch", "agent": "reference-researcher", "mode": "foundation"},
        {"kind": "launch", "agent": "reference-researcher", "mode": "validation"},
    ):
        canonical = canonical_execution_action(action)
        if canonical != action or build_semantic_subtask(canonical)["agent"] != action["agent"]:
            raise ControllerError(f"semantic action is not executable: {action!r}")
    final_validator_action = {
        "kind": "launch", "agent": "acceptance-validator", "mode": "final",
    }
    if canonical_execution_action(final_validator_action) != final_validator_action:
        raise ControllerError("final acceptance semantic action is not canonical")
    repair_prompt = PLANNER_PROMPTS["repair"].lower()
    if any(word in repair_prompt for word in ("jupiter", "fixture_probe", "horizons")):
        raise ControllerError("repair prompt is not project-generic")
    if unsupported_task_splitter_actions(
        [
            split_action,
            {"kind": "launch", "agent": "probe-builder", "deliverable": "D043"},
        ],
        canonical_execution_action(split_action),
    ):
        raise ControllerError("task-splitter slice rejected its selected action")
    if not unsupported_task_splitter_actions(
        [
            split_action,
            {"kind": "launch", "agent": "task-splitter", "deliverable": "D043", "generation": 1},
        ],
        canonical_execution_action(split_action),
    ):
        raise ControllerError("task-splitter slice allowed a second splitter action")

    try:
        build_implementation_subtask(
            {"kind": "launch", "agent": "task-splitter", "deliverable": "D042"},
            "SPLIT_PARENT: D042",
        )
    except ControllerError:
        pass
    else:
        raise ControllerError("task-splitter unexpectedly allowed by implementation slice")

    multi = [
        {"kind": "launch", "agent": "probe-builder", "deliverable": "D001"},
        {"kind": "launch", "agent": "probe-builder", "deliverable": "D002"},
        {"kind": "launch", "agent": "probe-builder", "deliverable": "D003"},
    ]
    if any(
        not (
            isinstance(action, dict)
            and action.get("kind") == "launch"
            and action.get("deliverable")
            and action.get("agent") != "task-splitter"
        )
        for action in multi
    ):
        raise ControllerError("multi-launch selftest unexpectedly rejected")

    action = {"kind": "launch", "agent": "probe-builder", "deliverable": "D001"}
    eid = execution_action_id("sv1", "ses-root", action)
    if eid != execution_action_id("sv1", "ses-root", dict(action)):
        raise ControllerError("execution action id is not deterministic")
    if eid == execution_action_id("sv2", "ses-root", action):
        raise ControllerError("execution action id did not bind state_version")

    intent = {
        "state_version": "sv1",
        "root_session": "ses-root",
        "action": action,
        "execution_id": eid,
        "baseline_attempt": {"count": 0, "sessions": []},
        "baseline_child_ids": [],
    }
    if reconcile_execution_evidence(intent, {"count": 0, "sessions": []}, []) is not None:
        raise ControllerError("empty replay evidence unexpectedly reconciled")
    ev = reconcile_execution_evidence(
        intent,
        {"count": 1, "sessions": ["dispatch:call:D001"]},
        [],
    )
    if not ev or ev.get("kind") != "preclaim-reservation":
        raise ControllerError(f"preclaim replay evidence mismatch: {ev!r}")
    ev = reconcile_execution_evidence(
        intent,
        {"count": 1, "sessions": ["ses-child"]},
        [{"id": "ses-child", "parentID": "ses-root", "agent": "probe-builder"}],
    )
    if not ev or ev.get("kind") != "bound-native-child":
        raise ControllerError(f"bound child replay evidence mismatch: {ev!r}")

    with tempfile.TemporaryDirectory(prefix="stage-a-controller-selftest-") as td:
        project = Path(td)
        ledger = load_execution_ledger(project)
        ledger["executions"][eid] = intent
        with execution_lock(project):
            save_execution_ledger(project, ledger)
        loaded = load_execution_ledger(project)
        if loaded != ledger:
            raise ControllerError("execution ledger atomic roundtrip mismatch")

    print("stage-a-controller selftest: OK")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Stage-A deterministic controller. Default operation is read-only; "
            "the explicitly gated implementation executor launches at most one "
            "native implementation SubtaskPart."
        )
    )
    ap.add_argument("--project", type=Path)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--poll", type=float, default=POLL_DEFAULT)
    ap.add_argument("--require-supervisor-shadow", action="store_true")
    ap.add_argument("--execute-first-implementation", action="store_true")
    ap.add_argument("--execute-first-task-splitter", action="store_true")
    ap.add_argument("--execute-first-planner", action="store_true")
    ap.add_argument("--execute-first-semantic", action="store_true")
    ap.add_argument("--execute-first-final-tests", action="store_true")
    ap.add_argument("--reconcile-execution", default="")
    ap.add_argument("--authorize-semantic-infrastructure-retry", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--base-url", default=os.environ.get("V2_OPENCODE_BASE_URL", ""))
    ap.add_argument("--root-session", default="")
    ap.add_argument("--selftest", action="store_true")
    ns = ap.parse_args()

    if ns.selftest:
        selftest()
        return 0

    if not ns.project:
        ap.error("--project is required unless --selftest is used")

    project = ns.project.resolve()
    if not project.is_dir():
        raise ControllerError(f"project does not exist: {project}")

    if ns.authorize_semantic_infrastructure_retry:
        if (
            ns.once or ns.watch or ns.execute_first_implementation
            or ns.execute_first_task_splitter or ns.execute_first_planner
            or ns.execute_first_semantic or ns.execute_first_final_tests
            or ns.reconcile_execution
        ):
            ap.error("--authorize-semantic-infrastructure-retry is standalone")
        if not ns.base_url:
            ap.error("--authorize-semantic-infrastructure-retry requires --base-url")
        receipt = authorize_semantic_infrastructure_retry(
            project,
            ns.base_url,
            ns.authorize_semantic_infrastructure_retry,
            ns.reason,
        )
        print(json.dumps(receipt, sort_keys=True, indent=2))
        return 0
    if ns.reason:
        ap.error("--reason requires --authorize-semantic-infrastructure-retry")

    if ns.reconcile_execution:
        if (ns.once or ns.watch or ns.execute_first_implementation
                or ns.execute_first_task_splitter or ns.execute_first_planner
                or ns.execute_first_semantic or ns.execute_first_final_tests):
            ap.error("--reconcile-execution is a standalone read-only operation")
        if not ns.base_url:
            intent = load_execution_ledger(project)["executions"].get(ns.reconcile_execution)
            if not isinstance(intent, dict) or (intent.get("action") or {}).get("kind") != "run_final_tests":
                ap.error("--reconcile-execution requires --base-url except for final tests")
        receipt = reconcile_execution(project, ns.base_url, ns.reconcile_execution)
        print(json.dumps(receipt, sort_keys=True, indent=2))
        return 0

    selected_executors=sum(bool(value) for value in (
        ns.execute_first_implementation,
        ns.execute_first_task_splitter,
        ns.execute_first_planner,
        ns.execute_first_semantic,
        ns.execute_first_final_tests,
    ))
    if selected_executors > 1:
        ap.error("choose at most one explicit executor")

    if ns.execute_first_implementation:
        if not ns.once or ns.watch:
            ap.error("--execute-first-implementation requires --once and forbids --watch")
        if not ns.require_supervisor_shadow:
            ap.error(
                "--execute-first-implementation currently requires "
                "--require-supervisor-shadow"
            )
        if not ns.base_url:
            ap.error("--execute-first-implementation requires --base-url")
        result = one_pass(project, True)
        receipt = execute_first_implementation(
            project,
            ns.base_url,
            result,
            explicit_root=ns.root_session,
        )
        print(json.dumps(receipt, sort_keys=True, indent=2))
        return 0

    if ns.execute_first_task_splitter:
        if not ns.once or ns.watch:
            ap.error("--execute-first-task-splitter requires --once and forbids --watch")
        if not ns.require_supervisor_shadow:
            ap.error(
                "--execute-first-task-splitter currently requires "
                "--require-supervisor-shadow"
            )
        if not ns.base_url:
            ap.error("--execute-first-task-splitter requires --base-url")
        result = one_pass(project, True)
        receipt = execute_first_task_splitter(
            project,
            ns.base_url,
            result,
            explicit_root=ns.root_session,
        )
        print(json.dumps(receipt, sort_keys=True, indent=2))
        return 0

    if ns.execute_first_planner:
        if not ns.once or ns.watch:
            ap.error("--execute-first-planner requires --once and forbids --watch")
        if not ns.require_supervisor_shadow:
            ap.error(
                "--execute-first-planner currently requires --require-supervisor-shadow"
            )
        if not ns.base_url:
            ap.error("--execute-first-planner requires --base-url")
        result = one_pass(project, True)
        receipt = execute_first_planner(
            project,
            ns.base_url,
            result,
            explicit_root=ns.root_session,
        )
        print(json.dumps(receipt, sort_keys=True, indent=2))
        return 0

    if ns.execute_first_semantic:
        if not ns.once or ns.watch:
            ap.error("--execute-first-semantic requires --once and forbids --watch")
        if not ns.require_supervisor_shadow:
            ap.error(
                "--execute-first-semantic currently requires --require-supervisor-shadow"
            )
        if not ns.base_url:
            ap.error("--execute-first-semantic requires --base-url")
        result = one_pass(project, True)
        receipt = execute_first_semantic(
            project,
            ns.base_url,
            result,
            explicit_root=ns.root_session,
        )
        print(json.dumps(receipt, sort_keys=True, indent=2))
        return 0

    if ns.execute_first_final_tests:
        if not ns.once or ns.watch:
            ap.error("--execute-first-final-tests requires --once and forbids --watch")
        if not ns.require_supervisor_shadow:
            ap.error(
                "--execute-first-final-tests currently requires "
                "--require-supervisor-shadow"
            )
        result = one_pass(project, True)
        receipt = execute_first_final_tests(project, result)
        print(json.dumps(receipt, sort_keys=True, indent=2))
        return 0

    if ns.once == ns.watch:
        ap.error("choose exactly one of --once or --watch")

    if ns.once:
        result = one_pass(project, ns.require_supervisor_shadow)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0

    if ns.poll <= 0:
        ap.error("--poll must be > 0")

    last_version = ""
    while True:
        try:
            result = one_pass(project, ns.require_supervisor_shadow)
            version = result["state_version"]
            if version != last_version:
                print(json.dumps(result, sort_keys=True), flush=True)
                last_version = version
        except ControllerError as exc:
            print(f"CONTROLLER_BLOCKED {exc}", file=sys.stderr, flush=True)
        time.sleep(ns.poll)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except ControllerError as exc:
        print(f"CONTROLLER_ERROR {exc}", file=sys.stderr)
        raise SystemExit(1)
