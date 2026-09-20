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
ROOT_SESSION_PROTOCOL = "v2-root-session-v1"
EXECUTION_LEDGER_PROTOCOL = "v2-stage-a-controller-execution-ledger-v1"
EXECUTION_RECEIPT_PROTOCOL = "v2-stage-a-controller-execute-v2"


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
    normalized = {
        "kind": str(action.get("kind") or ""),
        "agent": str(action.get("agent") or ""),
        "deliverable": str(action.get("deliverable") or ""),
    }
    if normalized["kind"] != "launch" or not normalized["agent"] or not normalized["deliverable"]:
        raise ControllerError(f"invalid implementation execution action: {action!r}")
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


def reconcile_execution(project: Path, base_url: str, execution_id: str) -> dict:
    with execution_lock(project):
        ledger = load_execution_ledger(project)
        intent = ledger["executions"].get(execution_id)
        if not isinstance(intent, dict):
            raise ControllerError(f"unknown execution_id={execution_id}")
        action = intent.get("action") or {}
        did = str(action.get("deliverable") or "")
        root = str(intent.get("root_session") or "")
        if not did or not root:
            raise ControllerError(f"execution intent incomplete: {execution_id}")
        attempts = attempt_snapshot(project, did)
        children = child_snapshot(project, base_url, root)
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
        if ns.once or ns.watch or ns.execute_first_implementation:
            ap.error("--reconcile-execution is a standalone read-only operation")
        if not ns.base_url:
            ap.error("--reconcile-execution requires --base-url")
        receipt = reconcile_execution(project, ns.base_url, ns.reconcile_execution)
        print(json.dumps(receipt, sort_keys=True, indent=2))
        return 0

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
