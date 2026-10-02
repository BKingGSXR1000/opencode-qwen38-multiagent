#!/usr/bin/env python3
"""Deterministic control-policy provenance.

The fingerprint is derived from the source modules that decide control
validity, readiness, scheduling, and deterministic dispatch. Any edit to those
modules changes the policy identity after process reload, invalidating stale
phase READY markers and changing materialized decision identity.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

_POLICY_FILES = (
    "control_policy.py",
    "acceptance_contract.py",
    "structured_plan.py",
    "leaf_contract.py",
    "control-guard.py",
    "control_state.py",
    "deterministic_dispatch.py",
    "control_query_views.py",
)

# Runtime code that must be reloaded by a long-lived supervisor, but must NOT
# invalidate Acceptance/Plan READY provenance merely because orchestration
# implementation details changed.
_SUPERVISOR_RUNTIME_FILES = (
    "supervisor.py",
    "worker_sandbox.py",
    "watchdog_telemetry.py",
    "state_io.py",
)


def reexec_source_paths():
    """All Python sources whose replacement must compile before supervisor exec."""
    root=Path(__file__).resolve().parent
    names=tuple(dict.fromkeys((*_POLICY_FILES,*_SUPERVISOR_RUNTIME_FILES)))
    return tuple(root/name for name in names)


def control_policy_fingerprint() -> str:
    root=Path(__file__).resolve().parent
    digest=hashlib.sha256()
    for name in _POLICY_FILES:
        path=root/name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def supervisor_runtime_fingerprint() -> str:
    root=Path(__file__).resolve().parent
    digest=hashlib.sha256()
    for name in _SUPERVISOR_RUNTIME_FILES:
        path=root/name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def control_policy_epoch() -> str:
    return "v2-control-policy-"+control_policy_fingerprint()[:20]


_ACCEPTANCE_POLICY_FILES = ("acceptance_contract.py",)
_ACCEPTANCE_GUARD_SYMBOLS = frozenset({
    "internal_external_reference_violation",
    "final_nonempty_line",
    "reference_policies",
    "validate_acceptance",
})
_ACCEPTANCE_GUARD_ASSIGNMENTS = frozenset({"ACC_MARKER"})


def _selected_source_bytes(path: Path, functions, assignments) -> bytes:
    source=path.read_text(encoding="utf-8")
    lines=source.splitlines(keepends=True)
    tree=ast.parse(source)
    chunks=[]
    for node in tree.body:
        include=(
            isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))
            and node.name in functions
        )
        if isinstance(node,(ast.Assign,ast.AnnAssign)):
            targets=node.targets if isinstance(node,ast.Assign) else [node.target]
            names={
                target.id for target in targets
                if isinstance(target,ast.Name)
            }
            include=include or bool(names.intersection(assignments))
        if include:
            chunks.append("".join(lines[node.lineno-1:node.end_lineno]))
    return "\n".join(chunks).encode("utf-8")


def acceptance_policy_fingerprint() -> str:
    """Fingerprint only semantics that can change Acceptance validity."""
    root=Path(__file__).resolve().parent
    digest=hashlib.sha256()
    for name in _ACCEPTANCE_POLICY_FILES:
        path=root/name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    guard=root/"control-guard.py"
    digest.update(b"control-guard.py:selected-acceptance-semantics\0")
    digest.update(_selected_source_bytes(
        guard,_ACCEPTANCE_GUARD_SYMBOLS,_ACCEPTANCE_GUARD_ASSIGNMENTS
    ))
    return digest.hexdigest()


def phase_ready_validator_id(artifact: str = "") -> str:
    if artifact=="ACCEPTANCE.md":
        return "deterministic-acceptance-"+acceptance_policy_fingerprint()[:20]
    return "deterministic-policy-"+control_policy_fingerprint()[:20]
