#!/usr/bin/env python3
"""Execute at most one action selected by the Stage-A deterministic controller."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import stage_a_controller as controller


class TickError(RuntimeError):
    pass


def action_route(actions: list[object]) -> str:
    """Classify an action set without deciding or changing its meaning."""
    launches = [item for item in actions if isinstance(item, dict) and item.get("kind") == "launch"]
    finals = [item for item in actions if isinstance(item, dict) and item.get("kind") == "run_final_tests"]
    benign = all(
        isinstance(item, dict) and item.get("kind") in {"launch", "run_final_tests", "wait", "rescan"}
        for item in actions
    )
    if len(finals) == 1 and not launches and benign:
        return "final-tests"
    if not launches:
        return "no-dispatch"
    semantic = [
        action for action in launches
        if (str(action.get("agent") or ""), str(action.get("mode") or ""))
        in controller.SEMANTIC_PROMPTS
    ]
    if len(semantic) == 1 and len(launches) == 1 and benign:
        return "semantic"
    if len(launches) == 1 and launches[0].get("agent") == "implementation-planner" and benign:
        return "planner"
    splitters = [action for action in launches if action.get("agent") == "task-splitter"]
    if len(splitters) == 1 and not semantic and all(
        str(action.get("agent") or "") != "implementation-planner" for action in launches
    ) and benign:
        return "task-splitter"
    if all(action.get("deliverable") and action.get("agent") != "task-splitter" for action in launches) and benign:
        return "implementation"
    raise TickError(f"unknown or mixed launch actions: {launches!r}")


def execute_one(project: Path, base_url: str, root_session: str = "") -> dict:
    result = controller.one_pass(project, True)
    route = action_route(result["actions"])
    if route == "no-dispatch":
        return {
            "protocol": "v2-stage-a-tick-v1",
            "state_version": result["state_version"],
            "outcome": "no-dispatch",
            "actions": result["actions"],
        }
    if route == "final-tests":
        receipt = controller.execute_first_final_tests(project, result)
    elif route == "semantic":
        receipt = controller.execute_first_semantic(project, base_url, result, root_session)
    elif route == "planner":
        receipt = controller.execute_first_planner(project, base_url, result, root_session)
    elif route == "task-splitter":
        receipt = controller.execute_first_task_splitter(project, base_url, result, root_session)
    else:
        receipt = controller.execute_first_implementation(project, base_url, result, root_session)
    return {"protocol": "v2-stage-a-tick-v1", "outcome": "dispatched", "receipt": receipt}


def selftest() -> None:
    cases = (
        ("semantic", [{"kind": "launch", "agent": "acceptance-planner", "mode": "fresh"}]),
        ("planner", [{"kind": "launch", "agent": "implementation-planner", "mode": "fresh"}]),
        ("task-splitter", [{"kind": "launch", "agent": "task-splitter", "deliverable": "D001", "generation": 1}]),
        ("implementation", [{"kind": "launch", "agent": "feature-builder", "deliverable": "D001"}]),
        ("implementation", [{"kind": "launch", "agent": "feature-builder", "deliverable": "D001"}, {"kind": "launch", "agent": "tester", "deliverable": "D002"}]),
        ("final-tests", [{"kind": "run_final_tests", "command": ".opencode-v2/bin/run-checks"}]),
        ("no-dispatch", [{"kind": "wait"}]),
    )
    for expected, actions in cases:
        actual = action_route(actions)
        if actual != expected:
            raise TickError(f"route mismatch: expected={expected} actual={actual}")
    try:
        action_route([{"kind": "launch", "agent": "unknown"}])
    except TickError:
        pass
    else:
        raise TickError("unknown launch was accepted")
    print("stage-a tick selftest: OK")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one supervisor-shadowed deterministic Stage-A action."
    )
    parser.add_argument("--project", type=Path)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--root-session", default="")
    parser.add_argument("--selftest", action="store_true")
    ns = parser.parse_args()
    if ns.selftest:
        selftest()
        return 0
    if ns.project is None or not ns.base_url:
        parser.error("--project and --base-url are required unless --selftest is used")
    try:
        receipt = execute_one(ns.project.resolve(), ns.base_url, ns.root_session)
    except controller.ControllerError as exc:
        raise TickError(str(exc)) from exc
    print(json.dumps(receipt, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TickError as exc:
        raise SystemExit(f"ERROR: {exc}")
