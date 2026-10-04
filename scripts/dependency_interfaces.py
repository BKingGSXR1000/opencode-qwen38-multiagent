#!/usr/bin/env python3
"""Small, deterministic API handoffs from verified direct dependencies.

Only bounded AST names and guarded contract text enter downstream contexts.
Never execute foreign source code, read entire model transcripts or trust
unverified dependency files for an interface claim.
"""
from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

MAX_DEPS=8
MAX_FILES_PER_DEP=5
MAX_SOURCE_BYTES=96000
MAX_PUBLIC_SYMBOLS=25
MAX_CONTRACT_CHARS=420
MAX_LITERAL_HINTS=12
MAX_LITERAL_HINT_LENGTH=32


def _safe_literal_return_hints(func):
    """Bounded syntactic literal observations, NEVER inferred runtime truth.

    Descend into if/try/loop branches of this function, but exclude nested
    functions/classes/lambdas; a nested return belongs to another function.
    A conditional expression contributes both literal arms. Refuse strings
    that could contain untrusted natural-language instructions.
    """
    values=set()

    def literals(node):
        if isinstance(node,ast.IfExp):
            literals(node.body)
            literals(node.orelse)
        elif isinstance(node,ast.Constant):
            value=node.value
            if (
                isinstance(value,str)
                and len(value)<=MAX_LITERAL_HINT_LENGTH
                and re.fullmatch(r"[A-Za-z0-9_.:/-]+",value)
            ):
                values.add(value)

    def walk(node):
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.Lambda,ast.ClassDef)):
            return
        if isinstance(node,ast.Return):
            literals(node.value)
            return
        for child in ast.iter_child_nodes(node):
            walk(child)

    for statement in func.body:
        walk(statement)
    return sorted(values)[:MAX_LITERAL_HINTS]


def python_public_interface(project, relative_path):
    """Return source-bound public Python declarations without executing code."""
    root=Path(project).resolve()
    rel=Path(str(relative_path))
    if (
        not relative_path or rel.is_absolute()
        or ".." in rel.parts or rel.suffix!=".py"
    ):
        return {}
    path=root/rel
    try:
        resolved=path.resolve(strict=True)
        resolved.relative_to(root)
        # Reject symlink files AND symlinked parent components.
        current=root
        for part in rel.parts:
            current=current/part
            if current.is_symlink():
                return {}
        if not resolved.is_file():
            return {}
        with resolved.open("rb") as handle:
            raw=handle.read(MAX_SOURCE_BYTES+1)
        if len(raw)>MAX_SOURCE_BYTES:
            return {}
        tree=ast.parse(raw.decode("utf-8"))
    except (OSError,ValueError,UnicodeError,SyntaxError):
        return {}

    functions=sorted({
        node.name for node in tree.body
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    })[:MAX_PUBLIC_SYMBOLS]
    classes=sorted({
        node.name for node in tree.body
        if isinstance(node,ast.ClassDef)
        and not node.name.startswith("_")
    })[:MAX_PUBLIC_SYMBOLS]
    hint_source={
        node.name:_safe_literal_return_hints(node)
        for node in tree.body
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))
        and node.name in functions
    }
    return {
        "path":rel.as_posix(),
        "sha256":hashlib.sha256(raw).hexdigest(),
        "public_functions":functions,
        "public_classes":classes,
        "literal_return_hints":{
            name:hint_source[name] for name in sorted(hint_source)
            if hint_source[name]
        },
        "literal_hint_authority":"static-AST-observation-not-verified-behavior",
    }


def direct_verified_interfaces(project, manifest, leaf, *, ready_lookup=None):
    """Project direct READY dependencies' exact declared, bounded public APIs."""
    if not isinstance(manifest,dict) or not isinstance(leaf,dict):
        return []
    leaves=manifest.get("leaves")
    if not isinstance(leaves,dict):
        return []
    if ready_lookup is None:
        # Planning/memory fixture projections may contain historical READY
        # marker files without an attempt ledger. They are not authoritative
        # live session attempts, so publish no verified API handoff. The
        # canonical state snapshot separately diagnoses a corrupt ledger.
        ledger=Path(project)/".opencode-v2"/"work"/"attempts.json"
        if not ledger.is_file():
            return []
        from control_state import ready_info
        ready_lookup=ready_info
    deps=[]
    for key in ("launch_deps","verify_deps","contract_deps"):
        for did in leaf.get(key,[]) if isinstance(leaf.get(key),list) else []:
            if isinstance(did,str) and did not in deps:
                deps.append(did)
    results=[]
    for did in deps[:MAX_DEPS]:
        upstream=leaves.get(did)
        if not isinstance(upstream,dict):
            continue
        if not ready_lookup(project,did):
            # Do not treat incomplete/invalidated files as authoritative.
            continue
        paths=upstream.get("owned_artifact_paths")
        if not isinstance(paths,list):
            paths=[]
        sources=[]
        for rel in paths[:MAX_FILES_PER_DEP]:
            if not isinstance(rel,str):
                continue
            api=python_public_interface(project,rel)
            if api:
                sources.append(api)
        results.append({
            "deliverable":did,
            "verified_ready":True,
            "outcome":str(upstream.get("outcome") or "")[:MAX_CONTRACT_CHARS],
            "done_when":str(upstream.get("done_when") or "")[:MAX_CONTRACT_CHARS],
            "python_interfaces":sources,
        })
    return results
