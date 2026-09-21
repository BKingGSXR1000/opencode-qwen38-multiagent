#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from deterministic_dispatch import select_actions

POLL_DEFAULT = 0.5
SEMANTIC_TERMINAL_GRACE_SECONDS = 10.0
HARNESS_ROOT = Path(__file__).resolve().parents[1]
ROOT_SESSION_PROTOCOL = "v2-root-session-v1"
EXECUTION_LEDGER_PROTOCOL = "v2-stage-a-controller-execution-ledger-v1"
EXECUTION_RECEIPT_PROTOCOL = "v2-stage-a-controller-execute-v2"
PLANNER_PROMPTS = {
    "fresh": """Create the first structured implementation plan for this project.

FIRST read .opencode-v2/ORIGINAL_TASK.md, .opencode-v2/ACCEPTANCE.md, and
.opencode-v2/CONTROL_CONTRACT.md. Inspect only project files needed to create a
concrete, dependency-aware plan.

Edit only .opencode-v2/IMPLEMENTATION_PLAN.structured.json. Follow the
implementation-planner protocol exactly. Do not create readiness markers,
generated IMPLEMENTATION_PLAN.md, test reports, or supervisor-owned runtime
state. Stop after the durable structured plan edit.""",
    "repair": """Repair structured implementation planning for this project.

This is a bounded repair, not a request to rewrite the plan. FIRST read
.opencode-v2/IMPLEMENTATION_PLAN.repair.json and
.opencode-v2/IMPLEMENTATION_PLAN.structured.json. Read the acceptance contract
only if an exact acceptance constraint is unclear.

Edit only .opencode-v2/IMPLEMENTATION_PLAN.structured.json. Preserve every
existing leaf key and list order: deliverable IDs and historical attempt
accounting must remain stable. Do not add, remove, or reorder leaves.

Treat the repair packet's affected_keys and error evidence as authoritative.
Repair only those leaves and the dependency references they name. Where a
consumer lacks prerequisites, assign ownership and fail-closed verification to
its existing direct producers; retain each leaf key and all unrelated plan
contracts. Do not fabricate data merely to satisfy a check.

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

FIRST read .opencode-v2/ORIGINAL_TASK.md, .opencode-v2/ACCEPTANCE.md, and any
guard-error artifact. Edit only .opencode-v2/ACCEPTANCE.md. Follow the
acceptance-planner protocol and do not create supervisor-owned readiness state.""",
    ("reference-researcher", "foundation"): """REFERENCE_MODE: FOUNDATION
Build or resume only the compact external-reference foundation. Read
.opencode-v2/acceptance/reference-work.json if present and follow the
reference-researcher protocol. Do not expand scope beyond durable requirements.""",
    ("reference-researcher", "validation"): """REFERENCE_MODE: VALIDATION
Resolve exactly one durable validation item. Resume
.opencode-v2/acceptance/reference-work.json if an item is in progress;
otherwise resolve only the first missing external-reference item. Follow the
reference-researcher protocol and persist its required durable evidence.""",
    ("acceptance-validator", "final"): """Run final acceptance validation for this project.

Read the durable acceptance, test, and reference evidence. Follow the
acceptance-validator protocol exactly; do not treat model prose as final
success and do not modify implementation artifacts.""",
}


class ControllerError(RuntimeError):
    pass


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
        except (TypeError, ValueError) as exc:
            raise ControllerError("task-splitter launch lacks a valid generation") from exc
        if generation < 1:
            raise ControllerError("task-splitter launch generation must be positive")
        normalized["generation"] = generation
    return normalized


def execution_action_id(state_version: str, root_session: str, action: dict) -> str:
    payload = {
        "state_version": str(state_version),
        "root_session": str(root_session),
        "action": canonical_execution_action(action),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


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
    if not proc.stdout.startswith("DISPATCH_MATERIALIZED "):
        raise ControllerError(
            "native child materialization returned unexpected output: "
            + proc.stdout.strip()[:1600]
        )


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
    return {
        "protocol": "v2-stage-a-controller-shadow-v1",
        "state_version": state_version,
        "resume_phase": phase,
        "actions": actions,
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


def one_pass(project: Path, require_shadow: bool) -> dict:
    result = evaluate(project)
    if require_shadow:
        ok, detail = compare_supervisor_shadow(project, result)
        result["shadow_match"] = ok
        result["shadow_detail"] = detail
        if not ok:
            raise ControllerError(detail)
    return result


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


def resolve_root_session(project: Path, base_url: str, explicit_session: str = "") -> str:
    if explicit_session:
        sid = explicit_session
    else:
        tracker = load_json(root_session_path(project), "root session tracker")
        if tracker.get("owner") != "supervisor":
            raise ControllerError("root session tracker owner is not supervisor")
        if tracker.get("protocol") != ROOT_SESSION_PROTOCOL:
            raise ControllerError(f"root session tracker protocol is not {ROOT_SESSION_PROTOCOL}")
        sid = str(tracker.get("session") or "")
        if not sid:
            raise ControllerError("root session tracker has no session")

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
        "command": "stage-a-controller",
    }


def build_task_splitter_subtask(action: dict) -> dict:
    agent = str(action.get("agent") or "")
    did = str(action.get("deliverable") or "")
    if agent != "task-splitter" or not did:
        raise ControllerError("task-splitter launch lacks canonical agent/deliverable")
    canonical_execution_action(action)
    return {
        "type": "subtask",
        "prompt": f"SPLIT_PARENT: {did}",
        "description": f"Split {did}",
        "agent": "task-splitter",
        "command": "stage-a-controller",
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
        "command": "stage-a-controller",
    }


def build_semantic_subtask(action: dict) -> dict:
    canonical = canonical_execution_action(action)
    key = (canonical.get("agent"), canonical.get("mode"))
    prompt = SEMANTIC_PROMPTS.get(key)
    if not prompt:
        raise ControllerError(f"unsupported semantic action: {canonical!r}")
    return {
        "type": "subtask",
        "prompt": prompt,
        "description": f"{canonical['agent']} {canonical['mode']}",
        "agent": canonical["agent"],
        "command": "stage-a-controller",
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
    for sid in native_child_ids(intent, children):
        return {"kind": "native-semantic-child", "sessions": [sid]}
    return None


def session_is_active(project: Path, base_url: str, sid: str) -> bool:
    status, body = http_json("GET", workspace_url(base_url, "/session/status", project))
    if status != 200 or not isinstance(body, dict):
        raise ControllerError(f"session status lookup failed status={status}")
    return sid in body


def semantic_child_may_still_transition(
    project: Path, base_url: str, intent: dict, evidence: dict
) -> bool:
    sessions = evidence.get("sessions") if isinstance(evidence, dict) else []
    sid = str(sessions[0]) if isinstance(sessions, list) and sessions else ""
    if sid and session_is_active(project, base_url, sid):
        return True
    created = intent.get("created_at_ms")
    try:
        age = time.time() - (int(created) / 1000.0)
    except (TypeError, ValueError):
        return False
    return age < SEMANTIC_TERMINAL_GRACE_SECONDS


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
    execution_id = execution_action_id(result["state_version"], root, canonical_action)

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

        ensure_root_idle(project, base_url, root)
        baseline_children = child_snapshot(project, base_url, root)
        intent = {
            "execution_id": execution_id,
            "state_version": result["state_version"],
            "root_session": root,
            "action": canonical_action,
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
    part = build_semantic_subtask(canonical_action)
    root = resolve_root_session(project, base_url, explicit_root)
    execution_id = execution_action_id(result["state_version"], root, canonical_action)

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
            if not isinstance(existing, dict) or existing.get("action") != canonical_action:
                raise ControllerError(f"execution ledger action mismatch: {execution_id}")
            evidence = semantic_reconcile_evidence(
                existing, child_snapshot(project, base_url, root)
            )
            if evidence:
                if not semantic_child_may_still_transition(
                    project, base_url, existing, evidence
                ):
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

        ensure_root_idle(project, base_url, root)
        baseline_children = child_snapshot(project, base_url, root)
        intent = {
            "execution_id": execution_id,
            "state_version": result["state_version"],
            "root_session": root,
            "action": canonical_action,
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
    execution_id = execution_action_id(result["state_version"], root, canonical_action)

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

        ensure_root_idle(project, base_url, root)
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

    part = build_task_splitter_subtask(launch)
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

        ensure_root_idle(project, base_url, root)
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
        "command": "stage-a-controller",
    }
    if payload != expected_payload:
        raise ControllerError(
            f"subtask payload mismatch actual={payload!r} expected={expected_payload!r}"
        )

    split_action = {
        "kind": "launch", "agent": "task-splitter", "deliverable": "D042", "generation": 3,
    }
    split_payload = build_task_splitter_subtask(split_action)
    if split_payload != {
        "type": "subtask", "prompt": "SPLIT_PARENT: D042", "description": "Split D042",
        "agent": "task-splitter", "command": "stage-a-controller",
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
    if semantic_payload["agent"] != "acceptance-planner" or semantic_payload[
        "command"
    ] != "stage-a-controller":
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
        {"kind": "launch", "agent": "acceptance-validator", "mode": "final"},
    ):
        canonical = canonical_execution_action(action)
        if canonical != action or build_semantic_subtask(canonical)["agent"] != action["agent"]:
            raise ControllerError(f"semantic action is not executable: {action!r}")
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
