#!/usr/bin/env python3
"""Read-only projection of the V2 filesystem control plane.

This module deliberately derives every value from the authoritative artifacts.
It does not write readiness, attempts, or test state.
"""
import json
from pathlib import Path


def _kv(path):
    try:
        return {
            key.strip(): value.strip()
            for line in path.read_text(errors="replace").splitlines()
            if "=" in line
            for key, value in [line.split("=", 1)]
        }
    except OSError:
        return {}


def ready_info(project, did):
    project = Path(project)
    data = _kv(project / ".opencode-v2" / "work" / f"{did}.ready")
    if (
        data.get("status") == "complete"
        and data.get("deliverable") == did
        and data.get("verified") == "true"
    ):
        return data
    return {}


def _phase_ready(project, name, artifact):
    data = _kv(Path(project) / ".opencode-v2" / name)
    return bool(data.get("status") == "complete" and data.get("artifact") == artifact)


def load_manifest(project):
    try:
        return json.loads(
            (Path(project) / ".opencode-v2" / "IMPLEMENTATION_PLAN.guard.json").read_text()
        )
    except (OSError, json.JSONDecodeError):
        return {}


def load_attempts(project):
    try:
        data = json.loads(
            (Path(project) / ".opencode-v2" / "work" / "attempts.json").read_text()
        )
        return data if isinstance(data, dict) else {"deliverables": {}}
    except (OSError, json.JSONDecodeError):
        return {"deliverables": {}}


def test_state(project):
    try:
        data = json.loads((Path(project) / ".opencode-v2" / "TEST_REPORT.json").read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    passed = data.get("status") == "pass" and isinstance(data.get("checks_run"), int) and data["checks_run"] > 0
    return {"complete": passed, "status": data.get("status"), "checks_run": data.get("checks_run", 0)}


def snapshot(project):
    """Return one derived state snapshot suitable for humans, scripts, or UI mirrors."""
    project = Path(project).resolve()
    manifest = load_manifest(project)
    leaves = manifest.get("leaves") if isinstance(manifest.get("leaves"), dict) else {}
    attempts = load_attempts(project).get("deliverables") or {}
    leaf_states = {}
    for did, leaf in sorted(leaves.items()):
        complete = bool(ready_info(project, did))
        entry = attempts.get(did) if isinstance(attempts.get(did), dict) else {}
        count = int(entry.get("count") or 0)
        deps = leaf.get("launch_deps") if isinstance(leaf, dict) else []
        deps = deps if isinstance(deps, list) else []
        missing = [dep for dep in deps if not ready_info(project, dep)]
        leaf_states[did] = {
            "complete": complete,
            "attempts": count,
            "attempt_limit_reached": count >= 3 and not complete,
            "launch_deps_missing": missing,
            "eligible": not complete and count < 3 and not missing,
        }
    acceptance_complete = _phase_ready(project, "ACCEPTANCE.ready", "ACCEPTANCE.md")
    plan_complete = _phase_ready(project, "IMPLEMENTATION_PLAN.ready", "IMPLEMENTATION_PLAN.md")
    tests = test_state(project)
    return {
        "protocol": "V2.6.8",
        "project": str(project),
        "acceptance": {"complete": acceptance_complete},
        "plan": {"complete": plan_complete, "manifest_present": bool(leaves)},
        "leaves": leaf_states,
        "tests": tests,
        "acceptance_validation": {
            "complete": (project / ".opencode-v2" / "acceptance-pass.json").exists()
        },
    }
