#!/usr/bin/env python3
"""Build a fresh one-leaf Stage-A fixture for live restart/recovery validation."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import deterministic_dispatch
import structured_plan
import supervisor
from state_io import atomic_write_json, atomic_write_text


class CanaryError(RuntimeError):
    pass


def acceptance_text() -> str:
    return """# Restart recovery canary acceptance

Reference policy: none

- [ ] A001: restart_probe.txt exists and contains exactly restart-recovery-ok.
- [ ] A002: The canonical final test manifest verifies the restart probe.

<!-- ACCEPTANCE_COMPLETE -->
"""


def structured_fixture() -> dict:
    return {
        "protocol": "v2-structured-plan-v1",
        "status": "complete",
        "leaves": [
            {
                "key": "restart_probe",
                "name": "Create restart recovery sentinel",
                "outcome": "restart_probe.txt contains exactly restart-recovery-ok.",
                "owned_artifacts": ["restart_probe.txt"],
                "launch_deps": [],
                "contract_deps": [],
                "verify_deps": [],
                "acceptance_ids": ["A001"],
                "complexity": "S",
                "repeated_operations": 0,
                "deep_reasoning": False,
                "role": "implementer",
                "verify_command": "test \"$(cat restart_probe.txt)\" = restart-recovery-ok",
                "done_when": "restart_probe.txt exists and contains exactly restart-recovery-ok.",
            },
            {
                "key": "final_tests",
                "name": "Record restart canary final check",
                "outcome": "The canonical final test manifest verifies restart_probe.txt.",
                "owned_artifacts": [".opencode-v2/TEST_CHECKS.json"],
                "launch_deps": ["restart_probe"],
                "contract_deps": [],
                "verify_deps": ["restart_probe"],
                "acceptance_ids": ["A002"],
                "complexity": "S",
                "repeated_operations": 0,
                "deep_reasoning": False,
                "role": "test-builder",
                "verify_command": ".opencode-v2/bin/run-checks",
                "done_when": "The canonical final test manifest passes.",
            }
        ],
    }


def build(project: Path, task_file: Path) -> dict:
    project = project.resolve()
    task_file = task_file.resolve()
    if not project.is_dir() or any(project.iterdir()):
        raise CanaryError("project must be a fresh empty directory")
    if not task_file.is_file():
        raise CanaryError("task file is missing")

    env = {**os.environ, "V2_ROOT": str(ROOT)}
    boot = subprocess.run(
        [
            sys.executable,
            str(HERE / "bootstrap-stage-a-project.py"),
            "--project",
            str(project),
            "--task-file",
            str(task_file),
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    if boot.returncode:
        raise CanaryError(f"bootstrap failed: {boot.stderr.strip() or boot.stdout.strip()}")

    ctrl = project / ".opencode-v2"
    atomic_write_text(ctrl / "ACCEPTANCE.md", acceptance_text())
    atomic_write_json(ctrl / "IMPLEMENTATION_PLAN.structured.json", structured_fixture())

    compiled, errors = structured_plan.compile_plan(project)
    if not compiled:
        raise CanaryError(f"structured fixture compile failed: {errors}")

    guarded = subprocess.run(
        [
            sys.executable,
            str(HERE / "control-guard.py"),
            "--project",
            str(project),
            "--finalize-all",
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    if guarded.returncode:
        raise CanaryError(
            "current control-state finalization failed: "
            + guarded.stdout
            + guarded.stderr
        )

    old_root, old_project = supervisor.ROOT, supervisor.PROJECT
    supervisor.ROOT = ROOT
    supervisor.PROJECT = str(project)
    try:
        supervisor.sync_control_status_snapshot()
    finally:
        supervisor.ROOT, supervisor.PROJECT = old_root, old_project

    decision = json.loads((ctrl / "query" / "decision.json").read_text())
    expected = {
        "kind": "launch",
        "agent": "implementer",
        "deliverable": "D001",
    }
    if decision.get("resume_phase") != "execution":
        raise CanaryError(
            f"restart canary is not in execution phase: {decision.get('resume_phase')!r}"
        )
    if decision.get("eligible") != ["D001"]:
        raise CanaryError(
            f"restart canary did not expose exactly D001 as eligible: {decision.get('eligible')!r}"
        )

    return {
        "protocol": "v2-restart-recovery-canary-v1",
        "project": str(project),
        "state_version": decision["state_version"],
        "action_after_scheduler_ready": expected,
        "resume_phase": decision["resume_phase"],
        "eligible": list(decision.get("eligible") or []),
        "scheduler_error": str((decision.get("scheduler") or {}).get("error") or ""),
    }


def selftest() -> None:
    with tempfile.TemporaryDirectory(prefix="restart-recovery-canary-") as td:
        root = Path(td)
        project = root / "project"
        project.mkdir()
        task = root / "TASK.md"
        task.write_text(
            "Create restart_probe.txt containing exactly restart-recovery-ok.\n",
            encoding="utf-8",
        )
        receipt = build(project, task)
        if receipt["resume_phase"] != "execution":
            raise CanaryError(f"unexpected phase: {receipt!r}")
    print("restart recovery canary fixture selftest: OK")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", type=Path)
    ap.add_argument("--task-file", type=Path)
    ap.add_argument("--selftest", action="store_true")
    ns = ap.parse_args()
    if ns.selftest:
        selftest()
        return 0
    if ns.project is None or ns.task_file is None:
        ap.error("--project and --task-file are required unless --selftest is used")
    print(json.dumps(build(ns.project, ns.task_file), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CanaryError as exc:
        raise SystemExit(f"ERROR: {exc}")
