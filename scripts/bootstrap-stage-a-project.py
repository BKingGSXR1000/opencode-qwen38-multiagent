#!/usr/bin/env python3
"""Create the durable, project-neutral inputs for a Stage-A run.

This command intentionally does not start OpenCode, the supervisor, or a
worker.  It only establishes the two inputs required before the deterministic
controller can select its first action: the immutable original task and the
repository-owned control-surface contract.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


HARNESS_ROOT = Path(__file__).resolve().parents[1]
RUN_CHECKS = HARNESS_ROOT / "scripts" / "run-checks.py"


class BootstrapError(RuntimeError):
    pass


def canonical_task(text: str) -> str:
    task = text.strip()
    if not task:
        raise BootstrapError("original task is empty")
    return task + "\n"


def read_task(path: Path) -> str:
    if not path.is_file():
        raise BootstrapError(f"task file is not a regular file: {path}")
    try:
        return canonical_task(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise BootstrapError(f"task file is not UTF-8 text: {path}") from exc


@contextmanager
def bootstrap_lock(project: Path):
    control = project / ".opencode-v2"
    control.mkdir(parents=True, exist_ok=True)
    path = control / ".stage-a-bootstrap.lock"
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write_new(path: Path, content: str) -> None:
    """Create an immutable input once, without partially written task text."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def bootstrap(project: Path, task: str, dry_run: bool = False) -> dict:
    project = project.resolve()
    if not project.is_dir():
        raise BootstrapError(f"project does not exist: {project}")
    if not RUN_CHECKS.is_file():
        raise BootstrapError(f"control-surface bootstrap is missing: {RUN_CHECKS}")
    control = project / ".opencode-v2"
    original = control / "ORIGINAL_TASK.md"
    digest = hashlib.sha256(task.encode("utf-8")).hexdigest()

    if dry_run:
        if original.exists():
            try:
                existing = original.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise BootstrapError(f"existing original task is not UTF-8: {original}") from exc
            if existing != task:
                raise BootstrapError(
                    "original task is already immutable and differs from the requested task; "
                    "start a new project run instead"
                )
            original_state = "preserved"
        else:
            original_state = "created"
        return {
            "protocol": "v2-stage-a-bootstrap-v1",
            "project": str(project),
            "original_task": original_state,
            "original_task_sha256": digest,
            "control_surface": "would-bootstrap",
        }

    with bootstrap_lock(project):
        if original.exists():
            try:
                existing = original.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise BootstrapError(f"existing original task is not UTF-8: {original}") from exc
            if existing != task:
                raise BootstrapError(
                    "original task is already immutable and differs from the requested task; "
                    "start a new project run instead"
                )
            original_state = "preserved"
        else:
            original_state = "created"

        if not original.exists():
            atomic_write_new(original, task)
        completed = subprocess.run(
            [sys.executable, str(RUN_CHECKS), "--project", str(project),
             "--bootstrap-control-contract"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0:
            raise BootstrapError(
                f"control-surface bootstrap failed with exit={completed.returncode}"
            )
        required = (control / "CONTROL_CONTRACT.md", control / "IMPLEMENTATION_PLAN.md")
        if not all(path.is_file() for path in required):
            raise BootstrapError("control-surface bootstrap did not create its required artifacts")
        return {
            "protocol": "v2-stage-a-bootstrap-v1",
            "project": str(project),
            "original_task": original_state,
            "original_task_sha256": digest,
            "control_surface": "ready",
        }


def selftest() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "arbitrary-project"
        project.mkdir()
        task_file = root / "task.md"
        task_file.write_text("Make a minimal arbitrary artifact.\n", encoding="utf-8")
        task = read_task(task_file)
        receipt = bootstrap(project, task)
        if receipt["original_task"] != "created" or receipt["control_surface"] != "ready":
            raise BootstrapError(f"bootstrap receipt mismatch: {receipt!r}")
        if (project / ".opencode-v2" / "ORIGINAL_TASK.md").read_text() != task:
            raise BootstrapError("original task was not persisted exactly")
        if bootstrap(project, task)["original_task"] != "preserved":
            raise BootstrapError("same immutable task was not preserved")
        try:
            bootstrap(project, canonical_task("a different task"))
        except BootstrapError:
            pass
        else:
            raise BootstrapError("different immutable task was accepted")
    print("stage-a bootstrap selftest: OK")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap a generic deterministic Stage-A project run."
    )
    parser.add_argument("--project", type=Path)
    parser.add_argument("--task-file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    ns = parser.parse_args()
    if ns.selftest:
        selftest()
        return 0
    if ns.project is None or ns.task_file is None:
        parser.error("--project and --task-file are required unless --selftest is used")
    receipt = bootstrap(ns.project, read_task(ns.task_file.resolve()), ns.dry_run)
    print(json.dumps(receipt, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BootstrapError as exc:
        raise SystemExit(f"ERROR: {exc}")
