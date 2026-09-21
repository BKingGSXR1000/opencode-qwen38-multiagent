#!/usr/bin/env python3
"""Read-only gates required before the Stage-A driver may dispatch work."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import stage_a_controller as controller
import stage_a_path_permissions as path_permissions


HARNESS_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_CONFIG_HOME = HARNESS_ROOT / "xdg" / "config"
PROOF_PROTOCOL = "v2-stage-a-consolidated-preflight-v1"


def _load_script_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {filename}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


root_create = _load_script_module("stage_a_root_create", "create-stage-a-root.py")
tick = _load_script_module("stage_a_tick", "run-stage-a-tick.py")


class PreflightError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PreflightError(message)


def read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(f"{label} is invalid: {path}") from exc
    require(isinstance(value, dict), f"{label} is not an object: {path}")
    return value


def source_audit(project: Path, task_file: Path) -> None:
    root = Path(subprocess.check_output(
        ["git", "-C", str(HARNESS_ROOT), "rev-parse", "--show-toplevel"], text=True
    ).strip()).resolve()
    require(root == HARNESS_ROOT.resolve(), "repository root identity mismatch")
    checked = subprocess.run(
        ["git", "-C", str(HARNESS_ROOT), "diff", "--check"],
        text=True, capture_output=True, check=False,
    )
    require(checked.returncode == 0, "repository has whitespace errors")
    require(project.is_dir(), f"project is not a directory: {project}")
    require(task_file.is_file(), f"task file is not a regular file: {task_file}")
    for name in (
        "stage_a_controller.py", "stage_a_path_permissions.py", "run-stage-a-tick.py",
        "create-stage-a-root.py", "worker_sandbox.py", "supervisor.py", "run-checks.py",
    ):
        require((HARNESS_ROOT / "scripts" / name).is_file(), f"required source missing: {name}")


def canonical_config_audit() -> None:
    config = CANONICAL_CONFIG_HOME / "opencode" / "opencode.jsonc"
    text = config.read_text(encoding="utf-8")
    require('"default_agent": "transport-root"' in text, "canonical default agent mismatch")
    require('"model": "v2noop/root-noop"' in text, "canonical root model mismatch")
    root_agent = (CANONICAL_CONFIG_HOME / "opencode" / "agents" / "transport-root.md").read_text(encoding="utf-8")
    require("model: v2noop/root-noop" in root_agent, "transport-root agent model mismatch")
    server = (HARNESS_ROOT / "scripts" / "run-a2-v11831-server.sh").read_text(encoding="utf-8")
    require("stage_a_path_permissions.py" in server, "server lacks exact-project permission overlay")
    require("mktemp -d /tmp/stage-a-opencode-config." in server, "server overlay is not ephemeral")


def backend_auth_audit() -> None:
    require(bool(os.environ.get("VLLM_API_KEY", "").strip()), "VLLM_API_KEY is unavailable")


def backend_get_audit() -> None:
    request = urllib.request.Request(
        "http://127.0.0.1:18033/v1/models",
        headers={"Authorization": f"Bearer {os.environ['VLLM_API_KEY']}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=4.0) as response:
            require(response.status == 200, f"backend models returned HTTP {response.status}")
            require(bool(response.read()), "backend models response is empty")
    except OSError as exc:
        raise PreflightError(f"backend/auth GET audit failed: {exc}") from exc


def fresh_root_request_audit(project: Path, base_url: str) -> None:
    seen = {}

    def fake_request(method: str, url: str, payload: dict):
        seen.update(method=method, url=url, payload=payload)
        return 201, {"id": "preflight-technical-root", "agent": "transport-root"}

    root_create.create_root(project, base_url, fake_request)
    require(seen.get("method") == "POST", "fresh-root request shape changed")
    require(seen.get("payload", {}).get("agent") == "transport-root", "fresh-root agent is not explicit")
    require(seen.get("payload", {}).get("model") == {"providerID": "v2noop", "id": "root-noop"}, "fresh-root model is not explicit")


def canonical_verify_audit() -> None:
    run = subprocess.run(
        [os.environ.get("PYTHON", "python3"), str(HARNESS_ROOT / "scripts" / "run-checks.py"), "--selftest"],
        text=True, capture_output=True, check=False,
    )
    require(run.returncode == 0, "canonical Verify selftest failed")
    require("run-checks selftest: OK" in run.stdout, "canonical Verify selftest did not report success")


def supervisor_binding_audit(project: Path) -> None:
    pid_file = project / ".opencode-v2" / "work" / "supervisor.pid"
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except (OSError, ValueError) as exc:
        raise PreflightError("project-scoped supervisor is unavailable") from exc
    expected = f"V2_PROJECT={project.resolve()}".encode()
    require(expected in environment, "supervisor project binding mismatch")


def opencode_identity_audit(project: Path, base_url: str, root_session: str, request=controller.http_json) -> Path:
    """Bind the live session directory to the same worktree semantics as projection."""
    status, body = request(
        "GET",
        controller.workspace_url(base_url, f"/session/{root_session}", project),
    )
    require(status == 200 and isinstance(body, dict), "live root session identity lookup failed")
    raw_directory = body.get("directory")
    require(isinstance(raw_directory, str) and raw_directory, "live root session directory is absent")
    actual_project = Path(raw_directory).resolve(strict=True)
    expected_project = project.resolve(strict=True)
    require(actual_project == expected_project, "live OpenCode project directory mismatch")
    actual_worktree = path_permissions.opencode_worktree(actual_project)
    expected_worktree = path_permissions.opencode_worktree(expected_project)
    require(actual_worktree == expected_worktree, "live OpenCode worktree identity mismatch")
    return actual_worktree


def owned_artifact_audit(project: Path, actions: list[object]) -> None:
    launches = [a for a in actions if isinstance(a, dict) and a.get("kind") == "launch" and a.get("deliverable")]
    if not launches:
        return
    manifest = read_json(project / ".opencode-v2" / "IMPLEMENTATION_PLAN.guard.json", "implementation manifest")
    leaves = manifest.get("leaves") if isinstance(manifest.get("leaves"), dict) else {}
    for action in launches:
        did = str(action["deliverable"])
        leaf = leaves.get(did)
        require(isinstance(leaf, dict), f"launch leaf is absent: {did}")
        owned = leaf.get("owned_artifacts")
        require(isinstance(owned, str) and owned.strip(), f"launch leaf has no owned artifacts: {did}")


def runtime_audit(project: Path, base_url: str, root_session: str) -> dict:
    backend_get_audit()
    supervisor_binding_audit(project)
    resolved = controller.resolve_root_session(project, base_url, root_session)
    require(resolved == root_session, "technical root session mismatch")
    worktree = opencode_identity_audit(project, base_url, root_session)
    result = controller.one_pass(project, True)
    route = tick.action_route(result["actions"])
    owned_artifact_audit(project, result["actions"])
    canonical_verify_audit()
    return {
        "state_version": result["state_version"], "actions": result["actions"],
        "route": route, "project": str(project.resolve()), "worktree": str(worktree),
    }


def write_proof(path: Path, project: Path, base_url: str, root_session: str, runtime: dict) -> None:
    now = int(time.time())
    proof = {
        "protocol": PROOF_PROTOCOL,
        "project": str(project.resolve()),
        "base_url": base_url.rstrip("/"),
        "root_session": root_session,
        "state_version": runtime["state_version"],
        "issued_at": now,
        "expires_at": now + 600,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proof, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def verify_proof(path: Path, project: Path, base_url: str, root_session: str) -> None:
    proof = read_json(path, "consolidated preflight proof")
    require(proof.get("protocol") == PROOF_PROTOCOL, "preflight proof protocol mismatch")
    require(proof.get("project") == str(project.resolve()), "preflight proof project mismatch")
    require(proof.get("base_url") == base_url.rstrip("/"), "preflight proof base URL mismatch")
    require(proof.get("root_session") == root_session, "preflight proof root mismatch")
    require(int(proof.get("expires_at") or 0) >= int(time.time()), "preflight proof expired")


def selftest() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        proof = project / "preflight-proof.json"
        runtime = {"state_version": "state-a", "actions": [], "route": "no-dispatch"}
        write_proof(proof, project, "http://127.0.0.1:57123", "ses-root", runtime)
        verify_proof(proof, project, "http://127.0.0.1:57123", "ses-root")
        try:
            verify_proof(proof, project, "http://127.0.0.1:57124", "ses-root")
        except PreflightError:
            pass
        else:
            raise AssertionError("wrong base URL was accepted")
        seen = {}
        def fake_request(method: str, url: str, payload=None):
            seen.update(method=method, url=url)
            return 200, {"directory": str(project)}
        require(opencode_identity_audit(project, "http://127.0.0.1:57123", "ses-root", fake_request) == Path("/"), "non-Git worktree audit failed")
        require(seen["method"] == "GET", "identity audit must not dispatch")
    print("stage-a preflight selftest: OK")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run non-dispatching Stage-A preflight gates.")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--task-file", type=Path)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--root-session", default="")
    parser.add_argument("--write-proof", type=Path)
    parser.add_argument("--selftest", action="store_true")
    ns = parser.parse_args()
    if ns.selftest:
        selftest()
        return 0
    if ns.project is None or ns.task_file is None or not ns.base_url:
        parser.error("--project, --task-file, and --base-url are required")
    project = ns.project.resolve()
    task_file = ns.task_file.resolve()
    source_audit(project, task_file)
    canonical_config_audit()
    backend_auth_audit()
    fresh_root_request_audit(project, ns.base_url)
    receipt = {"protocol": PROOF_PROTOCOL, "static": "PASS", "post_requests": 0, "worker_launches": 0}
    if ns.root_session:
        runtime = runtime_audit(project, ns.base_url, ns.root_session)
        receipt["runtime"] = runtime
        receipt["post_requests"] = 0
        receipt["worker_launches"] = 0
        if ns.write_proof is not None:
            write_proof(ns.write_proof, project, ns.base_url, ns.root_session, runtime)
            receipt["proof"] = str(ns.write_proof)
    elif ns.write_proof is not None:
        parser.error("--write-proof requires --root-session")
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PreflightError as exc:
        raise SystemExit(f"PREFLIGHT_FAIL: {exc}")
