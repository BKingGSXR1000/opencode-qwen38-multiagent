#!/usr/bin/env python3
"""V2 implementation-worker write firewall and transactional shell sandbox.

Direct editor tools are authorized against exact leaf ownership before OpenCode
executes them. Shell commands execute under bubblewrap with the real filesystem
read-only and a synthetic project view. Only declared owned artifacts and the
leaf progress file are copied back to the real project.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(os.environ.get("V2_ROOT", str(Path.home() / "AI/opencode-qwen38-multiagent-v2")))
DB = Path(os.environ.get("V2_OPENCODE_DB", str(ROOT / "xdg/data/opencode/opencode.db")))
SESSION_TABLE = os.environ.get("V2_OPENCODE_SESSION_TABLE", "session_v2")
IMPLEMENTATION_AGENTS = {
    "probe-builder","implementer","core-builder","feature-builder",
    "reasoning-builder","integrator","tester","test-builder",
}
EDIT_TOOLS = {"edit","write","apply_patch","patch","multiedit"}
EPHEMERAL_DIRS = (
    "node_modules",".venv","venv",".pytest_cache",".mypy_cache",".ruff_cache",
    ".cache","coverage","htmlcov",".nyc_output",".next",".vite",
)
EPHEMERAL_FILES = (".coverage",)
SANDBOX_ROOT = Path.home() / ".local/share/v2-worker-sandbox"
VERIFY_STAGE_ROOT = SANDBOX_ROOT / "verify-stage"
VALIDATOR_TMP_ROOT = Path(tempfile.gettempdir()) / f"v2-worker-validator-{os.getuid()}"
# V2.6.9 BATCH8 VERIFY-SANDBOX-LIFETIME-V3
# V2.6.9 BATCH9A CONCURRENT-SHADOW-GUARD
# V2.6.16 RESOLVER-SNAPSHOT-WITH-HOST-IPC-ISOLATION


class SandboxError(RuntimeError):
    pass


def _atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_name(path.name+f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data,indent=2,sort_keys=True)+"\n")
    os.replace(tmp,path)


def _safe_session_token(session: str):
    token=re.sub(r"[^A-Za-z0-9_.-]+","_",session or "unknown")
    return token[:160] or "unknown"


def _parse_did(text: str):
    m=re.search(r"(?mi)^\s*DELIVERABLE\s*:\s*(D\d{3}(?:-[AB](?:[12])?)?)\s*$",text or "")
    return m.group(1) if m else ""


def _db_connect():
    return sqlite3.connect(f"file:{DB}?mode=ro",uri=True,timeout=1)

def _v1_history_enabled():
    return SESSION_TABLE=="session"

def _v1_first_user_text(session: str):
    con=_db_connect()
    try:
        rows=con.execute(
            "SELECT id,data FROM message "
            "WHERE session_id=? ORDER BY time_created,id",
            (session,),
        ).fetchall()
        for mid,raw in rows:
            try:
                info=json.loads(raw) if raw else {}
            except Exception:
                continue
            if not isinstance(info,dict) or info.get("role")!="user":
                continue
            parts=con.execute(
                "SELECT data FROM part WHERE message_id=? ORDER BY time_created,id",
                (mid,),
            ).fetchall()
            texts=[]
            for (part_raw,) in parts:
                try:
                    part=json.loads(part_raw) if part_raw else {}
                except Exception:
                    continue
                if (
                    isinstance(part,dict)
                    and part.get("type")=="text"
                    and isinstance(part.get("text"),str)
                ):
                    texts.append(part["text"])
            return "\n".join(texts)
        return ""
    finally:
        con.close()



def _session_agent(session: str):
    if not session or not DB.exists():
        return ""
    try:
        con=_db_connect()
        row=con.execute(
            f"SELECT coalesce(agent,'') FROM {SESSION_TABLE} WHERE id=?",
            (session,),
        ).fetchone()
        con.close()
        return str(row[0] or "") if row else ""
    except Exception:
        return ""


def _first_user_text(session: str):
    if not session or not DB.exists():
        return ""
    if _v1_history_enabled():
        try:
            return _v1_first_user_text(session)
        except Exception:
            return ""
    try:
        con=_db_connect()
        row=con.execute(
            "SELECT data FROM session_message "
            "WHERE session_id=? AND type='user' ORDER BY seq LIMIT 1",
            (session,),
        ).fetchone()
        con.close()
        if not row:
            return ""
        data=json.loads(row[0])
        return data.get("text","") if isinstance(data,dict) else ""
    except Exception:
        return ""

def _session_from_call_id(call_id: str):
    if not call_id or not DB.exists():
        return ""
    if _v1_history_enabled():
        needle=f"%{call_id}%"
        try:
            con=_db_connect()
            rows=con.execute(
                "SELECT session_id,data FROM part "
                "WHERE data LIKE ? ORDER BY time_created DESC,id DESC LIMIT 64",
                (needle,),
            ).fetchall()
            con.close()
        except Exception:
            return ""
        for sid,raw in rows:
            try:
                data=json.loads(raw)
            except Exception:
                continue
            if not isinstance(data,dict) or data.get("type")!="tool":
                continue
            if call_id in {
                str(data.get("callID","")),
                str(data.get("callId","")),
                str(data.get("id","")),
            }:
                return str(sid or "")
        return ""
    needle=f"%{call_id}%"
    try:
        con=_db_connect()
        rows=con.execute(
            "SELECT session_id,data FROM session_message "
            "WHERE type='assistant' AND data LIKE ? ORDER BY rowid DESC LIMIT 32",
            (needle,),
        ).fetchall()
        con.close()
    except Exception:
        return ""
    for sid,raw in rows:
        try:
            data=json.loads(raw)
        except Exception:
            continue
        stack=[data]
        while stack:
            item=stack.pop()
            if isinstance(item,dict):
                if call_id in {
                    str(item.get("id","")),
                    str(item.get("callID","")),
                    str(item.get("callId","")),
                    str(item.get("toolCallId","")),
                }:
                    return sid
                stack.extend(item.values())
            elif isinstance(item,list):
                stack.extend(item)
    return ""

def load_json(path: Path, label: str):
    try:
        data=json.loads(path.read_text())
    except FileNotFoundError:
        raise SandboxError(f"{label} missing: {path}")
    except Exception as exc:
        raise SandboxError(f"{label} unreadable: {exc}")
    if not isinstance(data,dict):
        raise SandboxError(f"{label} must be a JSON object")
    return data


def _manifest(project: Path):
    return load_json(
        project/".opencode-v2/IMPLEMENTATION_PLAN.guard.json",
        "implementation manifest",
    )


def _attempts(project: Path):
    data=load_json(project/".opencode-v2/work/attempts.json","attempt ledger")
    if data.get("owner") != "supervisor":
        raise SandboxError("attempt ledger owner is not supervisor")
    return data


def resolve_worker(project: Path, session: str="", call_id: str="", agent: str=""):
    session=session or _session_from_call_id(call_id)
    if not session:
        return {"worker":False,"session":"","did":"","agent":agent,"reason":"session-unresolved"}

    # The mutation hook runs for every OpenCode session, not only implementation
    # workers. Phase planners/validators legitimately write control artifacts
    # before the implementation manifest and attempt ledger exist. Classify a
    # known non-implementation role before touching implementation-only state.
    role_hint=agent or _session_agent(session)
    if role_hint and role_hint not in IMPLEMENTATION_AGENTS:
        return {
            "worker":False,"session":session,"did":"","agent":role_hint,
            "reason":"non-implementation-role",
        }

    manifest_path=project/".opencode-v2/IMPLEMENTATION_PLAN.guard.json"
    attempts_path=project/".opencode-v2/work/attempts.json"
    if not manifest_path.exists() and not attempts_path.exists():
        # Before Phase 0.5 has produced executable implementation state there
        # cannot be a legitimate implementation worker: dispatch preclaim itself
        # depends on that state. Let ordinary OpenCode permissions govern control
        # sessions such as acceptance-planner / implementation-planner.
        return {
            "worker":False,"session":session,"did":"","agent":role_hint,
            "reason":"pre-implementation-control-phase",
        }

    # Once implementation state exists, missing/corrupt canonical state must
    # remain fail-closed. This preserves the Batch-2 durability invariant.
    attempts=_attempts(project)
    did=""
    attempt=0
    for candidate,entry in (attempts.get("deliverables") or {}).items():
        if not isinstance(entry,dict):
            continue
        sessions=entry.get("sessions")
        if isinstance(sessions,list) and session in sessions:
            did=candidate
            try:
                attempt=int(entry.get("count") or 0)
            except Exception:
                attempt=0
            break

    if not did:
        did=_parse_did(_first_user_text(session))

    if not did:
        if role_hint in IMPLEMENTATION_AGENTS:
            raise SandboxError(
                f"implementation session {session} has no resolved deliverable yet"
            )
        raise SandboxError(
            f"WORKER_FIREWALL_CONTEXT_UNKNOWN cannot classify mutation session {session}"
        )

    manifest=_manifest(project)
    leaf=(manifest.get("leaves") or {}).get(did)
    if not isinstance(leaf,dict):
        raise SandboxError(f"deliverable {did} missing from manifest")
    role=str(leaf.get("role") or agent or "")
    if role not in IMPLEMENTATION_AGENTS:
        return {"worker":False,"session":session,"did":did,"agent":role,"reason":"non-implementation-role"}

    if attempt <= 0:
        entry=(attempts.get("deliverables") or {}).get(did) or {}
        try:
            attempt=int(entry.get("count") or 0)
        except Exception:
            attempt=0

    return {
        "worker":True,
        "session":session,
        "did":did,
        "agent":role,
        "attempt":attempt,
        "leaf":leaf,
    }


def _normalize_rel(project: Path, raw: str):
    if not isinstance(raw,str) or not raw.strip():
        raise SandboxError("empty mutation path")
    raw=raw.strip()
    p=Path(raw)
    if p.is_absolute():
        try:
            rel=p.resolve(strict=False).relative_to(project.resolve())
        except Exception:
            raise SandboxError(f"path escapes project: {raw}")
    else:
        rel=Path(os.path.normpath(raw))
    posix=rel.as_posix()
    if posix in ("",".") or posix==".." or posix.startswith("../"):
        raise SandboxError(f"path escapes project: {raw}")
    return posix


def owned_paths(ctx):
    leaf=ctx["leaf"]
    result=[]
    for raw in leaf.get("owned_artifact_paths",[]) or []:
        if not isinstance(raw,str):
            continue
        result.append(raw)
    result.append(f".opencode-v2/work/{ctx['did']}.progress.md")
    return list(dict.fromkeys(result))


def _strip_dir_marker(path: str):
    return path[:-1] if path.endswith("/") else path


def path_is_owned(rel: str, declared):
    """Match canonical ownership roots using supervisor containment semantics."""
    rel=rel.rstrip("/")
    for raw in declared:
        base=_strip_dir_marker(raw).rstrip("/")
        if not base:
            continue
        if rel==base or rel.startswith(base+"/"):
            return True
    return False


def assert_no_symlink_components(project: Path, rel: str):
    """Ownership is lexical; filesystem aliases must not redirect it."""
    base=project.resolve()
    clean=_strip_dir_marker(rel).rstrip("/")
    if not clean:
        raise SandboxError("empty owned path")
    current=base
    for part in PurePosixPath(clean).parts:
        current=current/part
        if current.is_symlink():
            raise SandboxError(f"owned/mutation path traverses symlink: {rel}")
    return clean


def path_is_ancestor(rel: str, declared):
    rel=rel.rstrip("/")
    prefix=rel+"/" if rel else ""
    for raw in declared:
        base=_strip_dir_marker(raw).rstrip("/")
        if base.startswith(prefix) and base!=rel:
            return True
    return False


def path_is_ephemeral(rel: str):
    rel=rel.rstrip("/")
    return (
        any(rel==base or rel.startswith(base+"/") for base in EPHEMERAL_DIRS)
        or rel in EPHEMERAL_FILES
    )


def violation_path(project: Path, did: str, session: str):
    return (
        project/".opencode-v2/work/sandbox-violations"/
        f"{did}.{_safe_session_token(session)}.jsonl"
    )


FATAL_VIOLATION_KINDS={"sandbox-outside-ownership"}
DENIED_PREEXECUTION_VIOLATION_KINDS={
    "direct-tool-outside-ownership",
    "direct-tool-symlink-ownership",
    "forbidden-code-mode",
    "manual-sandbox-wrapper",
}


def violation_is_fatal(record):
    return (
        isinstance(record,dict)
        and str(record.get("kind") or "") in FATAL_VIOLATION_KINDS
    )


def violation_records(project: Path, did: str, session: str):
    path=violation_path(project,did,session)
    if not path.exists() or not path.stat().st_size:
        return []
    records=[]
    try:
        for line in path.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            record=json.loads(line)
            if not isinstance(record,dict):
                raise ValueError("violation record is not an object")
            records.append(record)
    except (OSError,ValueError,json.JSONDecodeError) as exc:
        raise SandboxError(
            f"invalid sandbox violation audit for {did}/{session}: {exc}"
        ) from exc
    return records


def has_fatal_violation(project: Path, did: str, session: str):
    try:
        return any(
            violation_is_fatal(record)
            for record in violation_records(project,did,session)
        )
    except SandboxError:
        # Corrupt audit evidence fails closed.
        return True


def has_only_denied_preexecution_violations(
    project: Path, did: str, session: str
):
    try:
        records=violation_records(project,did,session)
    except SandboxError:
        return False
    return bool(records) and all(
        str(record.get("kind") or "") in DENIED_PREEXECUTION_VIOLATION_KINDS
        for record in records
    )


def record_violation(project: Path, ctx, kind: str, detail):
    path=violation_path(project,ctx["did"],ctx["session"])
    path.parent.mkdir(parents=True,exist_ok=True)
    record={
        "timestamp":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
        "deliverable":ctx["did"],
        "session":ctx["session"],
        "attempt":ctx.get("attempt",0),
        "agent":ctx.get("agent",""),
        "kind":kind,
        "detail":detail,
    }
    with path.open("a",encoding="utf-8") as fh:
        fh.write(json.dumps(record,sort_keys=True)+"\n")
    return path


def authorize_paths(project: Path, ctx, paths, tool: str):
    declared=owned_paths(ctx)
    normalized=[]
    for raw in paths:
        rel=_normalize_rel(project,raw)
        normalized.append(rel)
        if not path_is_owned(rel,declared):
            record_violation(project,ctx,"direct-tool-outside-ownership",{
                "tool":tool,"path":rel,"owned":declared,
            })
            raise SandboxError(
                f"WORKER_FIREWALL_DENY {ctx['did']} {tool} path outside ownership: {rel}"
            )
        try:
            assert_no_symlink_components(project,rel)
        except SandboxError:
            record_violation(project,ctx,"direct-tool-symlink-ownership",{
                "tool":tool,"path":rel,"owned":declared,
            })
            raise
    return normalized


def patch_paths(patch_text: str):
    if not isinstance(patch_text,str):
        raise SandboxError("apply_patch patchText missing")
    paths=[]
    rx=re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to):\s*(.+?)\s*$",re.M)
    for match in rx.finditer(patch_text):
        value=match.group(1).strip()
        if value:
            paths.append(value)
    if not paths:
        raise SandboxError("apply_patch contains no recognized file paths")
    return list(dict.fromkeys(paths))


def mutation_paths(tool: str, args):
    if not isinstance(args,dict):
        raise SandboxError("tool args must be an object")
    if tool in {"edit","write","multiedit"}:
        value=args.get("filePath") or args.get("filepath") or args.get("path")
        if isinstance(value,str):
            return [value]
        files=args.get("files")
        if isinstance(files,list):
            out=[]
            for item in files:
                if isinstance(item,dict):
                    value=item.get("filePath") or item.get("path")
                    if isinstance(value,str):
                        out.append(value)
            if out:
                return out
        raise SandboxError(f"{tool} has no filePath")
    if tool in {"apply_patch","patch"}:
        return patch_paths(args.get("patchText") or args.get("patch") or "")
    return []


def _lower_target(rel: str, lower_root="/v2-lower"):
    root=str(lower_root).rstrip("/") or "/"
    return root+"/"+rel if rel else root


def _copy_owned(src: Path, dst: Path, declared_dir: bool):
    if not src.exists() and not src.is_symlink():
        if declared_dir:
            dst.mkdir(parents=True,exist_ok=True)
        return
    dst.parent.mkdir(parents=True,exist_ok=True)
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src,dst,symlinks=True)
    elif src.is_symlink():
        dst.symlink_to(os.readlink(src))
    else:
        shutil.copy2(src,dst)


def build_shadow(project: Path, shadow: Path, declared, lower_root="/v2-lower", enforce_no_symlink=True):
    project=project.resolve()
    shadow.mkdir(parents=True,exist_ok=True)
    declared=list(dict.fromkeys(declared))
    if enforce_no_symlink:
        for raw in declared:
            assert_no_symlink_components(project,_strip_dir_marker(raw))

    def recurse(rel_dir: str):
        lower_dir=project/rel_dir if rel_dir else project
        shadow_dir=shadow/rel_dir if rel_dir else shadow
        shadow_dir.mkdir(parents=True,exist_ok=True)
        existing={}
        if lower_dir.is_dir():
            try:
                existing={p.name:p for p in lower_dir.iterdir()}
            except OSError:
                existing={}

        child_names=set(existing)
        prefix=rel_dir.rstrip("/")+"/" if rel_dir else ""
        for raw in declared:
            base=_strip_dir_marker(raw)
            if prefix and not base.startswith(prefix):
                continue
            rest=base[len(prefix):] if base.startswith(prefix) else base
            if rest:
                child_names.add(rest.split("/",1)[0])

        for name in sorted(child_names):
            rel=f"{prefix}{name}" if prefix else name
            s=shadow/name if not rel_dir else shadow_dir/name
            lower=project/rel
            matching=[raw for raw in declared if _strip_dir_marker(raw)==rel]
            if matching:
                declared_dir=any(raw.endswith("/") for raw in matching)
                _copy_owned(lower,s,declared_dir)
            elif path_is_ephemeral(rel):
                if rel in EPHEMERAL_FILES:
                    s.parent.mkdir(parents=True,exist_ok=True)
                    s.touch(exist_ok=True)
                else:
                    s.mkdir(parents=True,exist_ok=True)
            elif path_is_ancestor(rel,declared):
                recurse(rel)
            else:
                if s.exists() or s.is_symlink():
                    continue
                s.symlink_to(_lower_target(rel,lower_root))
    recurse("")
    return shadow


def _runtime_state_path(session: str):
    return SANDBOX_ROOT/"state"/(_safe_session_token(session)+".json")


def _mark_runtime_state(project: Path, ctx):
    _atomic_json(_runtime_state_path(ctx["session"]),{
        "protocol":"v2-worker-sandbox-runtime-v1",
        "project":str(project.resolve()),
        "session":ctx["session"],
        "deliverable":ctx.get("did",""),
        "agent":ctx.get("agent",""),
        "attempt":int(ctx.get("attempt") or 0),
        "used_bash":True,
    })


def session_runtime_state(session: str):
    path=_runtime_state_path(session)
    if not path.exists():
        return {}
    try:
        data=json.loads(path.read_text())
    except Exception as exc:
        raise SandboxError(
            f"worker sandbox runtime marker corrupt for {session}: {type(exc).__name__}"
        ) from exc
    if not isinstance(data,dict) or data.get("protocol")!="v2-worker-sandbox-runtime-v1":
        raise SandboxError(f"worker sandbox runtime marker invalid for {session}")
    return data


def session_used_sandbox(session: str):
    return bool(session_runtime_state(session).get("used_bash"))


def _ephemeral_scratch(session: str):
    base=SANDBOX_ROOT/"scratch"/_safe_session_token(session)
    base.mkdir(parents=True,exist_ok=True)
    return base


def _session_tmpdir(session: str):
    """Private /tmp that persists for one worker session only.

    OpenCode executes each shell tool as a separate Bubblewrap process. A fresh
    --tmpfs /tmp on every process made ordinary multi-command workflows lose
    downloads/probe state between tool calls. Keep the mount private per
    session, but preserve it until supervisor terminal cleanup.
    """
    path=_ephemeral_scratch(session)/"tmp"
    path.mkdir(parents=True,exist_ok=True)
    os.chmod(path,0o1777)
    return path


def _seed_ephemeral_dir(project: Path, rel: str, dst: Path, lower_root="/v2-lower"):
    if dst.exists():
        return
    dst.mkdir(parents=True,exist_ok=True)
    lower=project/rel
    if not lower.is_dir():
        return
    for child in lower.iterdir():
        target=dst/child.name
        if child.is_dir() and child.name.startswith("@"):
            target.mkdir()
            for grand in child.iterdir():
                (target/grand.name).symlink_to(_lower_target(f"{rel}/{child.name}/{grand.name}",lower_root))
        else:
            target.symlink_to(_lower_target(f"{rel}/{child.name}",lower_root))


def _ephemeral_mounts(project: Path, session: str, lower_root="/v2-lower"):
    scratch=_ephemeral_scratch(session)
    mounts=[]
    for rel in EPHEMERAL_DIRS:
        dst=scratch/rel
        _seed_ephemeral_dir(project,rel,dst,lower_root)
        mounts.append(("dir",dst,project/rel))
    for rel in EPHEMERAL_FILES:
        dst=scratch/rel
        dst.parent.mkdir(parents=True,exist_ok=True)
        if not dst.exists():
            lower=project/rel
            if lower.is_file():
                shutil.copy2(lower,dst)
            else:
                dst.touch()
        mounts.append(("file",dst,project/rel))
    return mounts


def snapshot_unowned_shadow_guards(shadow: Path, declared, lower_root="/v2-lower"):
    """Capture the immutable guard structure created before a worker command.

    Never derive the post-command expected set from the live shared project:
    supervisor/other-worker commits may legitimately change that tree while this
    worker is running.
    """
    declared=list(declared)
    links={}
    ancestors=set()

    def recurse(rel_dir: str):
        shadow_dir=shadow/rel_dir if rel_dir else shadow
        if not shadow_dir.is_dir():
            return
        prefix=rel_dir.rstrip("/")+"/" if rel_dir else ""
        for child in shadow_dir.iterdir():
            name=child.name
            rel=f"{prefix}{name}" if prefix else name
            if path_is_owned(rel,declared) or path_is_ephemeral(rel):
                continue
            if path_is_ancestor(rel,declared):
                if not child.is_dir() or child.is_symlink():
                    raise SandboxError(
                        f"worker shadow ancestor is not a directory: {rel}"
                    )
                ancestors.add(rel)
                recurse(rel)
                continue
            if not child.is_symlink():
                raise SandboxError(
                    f"worker shadow guard is not a symlink before execution: {rel}"
                )
            links[rel]=os.readlink(child)
    recurse("")
    return {"links":links,"ancestors":sorted(ancestors)}


def detect_unowned_shadow_changes(shadow: Path, declared, guards):
    """Detect worker mutations only against the command-start shadow snapshot.

    This intentionally ignores changes to the live shared project after the
    sandbox is built, preventing concurrent supervisor/worker state from being
    falsely attributed to this command.
    """
    violations=[]
    declared=list(declared)
    expected_links=dict((guards or {}).get("links") or {})
    expected_ancestors=set((guards or {}).get("ancestors") or [])
    seen_links=set()
    seen_ancestors=set()

    def recurse(rel_dir: str):
        shadow_dir=shadow/rel_dir if rel_dir else shadow
        if not shadow_dir.is_dir():
            return
        prefix=rel_dir.rstrip("/")+"/" if rel_dir else ""
        for child in shadow_dir.iterdir():
            name=child.name
            rel=f"{prefix}{name}" if prefix else name
            if path_is_owned(rel,declared) or path_is_ephemeral(rel):
                continue
            if path_is_ancestor(rel,declared):
                seen_ancestors.add(rel)
                if rel not in expected_ancestors or not child.is_dir() or child.is_symlink():
                    violations.append(rel)
                else:
                    recurse(rel)
                continue
            seen_links.add(rel)
            expected=expected_links.get(rel)
            if expected is None or not child.is_symlink():
                violations.append(rel)
                continue
            try:
                if os.readlink(child)!=expected:
                    violations.append(rel)
            except OSError:
                violations.append(rel)

    recurse("")
    for rel in expected_links:
        if rel not in seen_links:
            violations.append(rel)
    for rel in expected_ancestors:
        if rel not in seen_ancestors:
            violations.append(rel)
    return sorted(set(violations))


def _replace_path(src: Path, dst: Path):
    if not src.exists() and not src.is_symlink():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        elif dst.exists() or dst.is_symlink():
            dst.unlink()
        return
    dst.parent.mkdir(parents=True,exist_ok=True)
    if src.is_dir() and not src.is_symlink():
        temp=dst.parent/(dst.name+f".v2merge.{os.getpid()}")
        if temp.exists():
            shutil.rmtree(temp)
        shutil.copytree(src,temp,symlinks=True)
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        elif dst.exists() or dst.is_symlink():
            dst.unlink()
        os.replace(temp,dst)
    elif src.is_symlink():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        elif dst.exists() or dst.is_symlink():
            dst.unlink()
        dst.symlink_to(os.readlink(src))
    else:
        fd,tmp=tempfile.mkstemp(prefix="."+dst.name+".v2merge.",dir=str(dst.parent))
        os.close(fd)
        shutil.copy2(src,tmp)
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        os.replace(tmp,dst)


def merge_owned(project: Path, shadow: Path, declared):
    merged=[]
    for raw in declared:
        rel=_strip_dir_marker(raw)
        assert_no_symlink_components(project,rel)
        src=shadow/rel
        dst=project/rel
        _replace_path(src,dst)
        merged.append(rel)
    return merged


def _verify_stage_dir(session: str):
    return VERIFY_STAGE_ROOT/_safe_session_token(session)

def stage_verify_outputs(shadow: Path, session: str):
    src=shadow/".opencode-v2/TEST_REPORT.json"
    if not src.is_file() or src.is_symlink():
        raise SandboxError("canonical run-checks Verify produced no regular TEST_REPORT.json")
    try: data=json.loads(src.read_text())
    except Exception as exc: raise SandboxError(f"staged TEST_REPORT.json is invalid: {exc}") from exc
    if data.get("status")!="pass" or not isinstance(data.get("checks_run"),int) or data.get("checks_run",0)<=0:
        raise SandboxError("staged TEST_REPORT.json is not a passing report")
    stage=_verify_stage_dir(session)
    if stage.exists(): shutil.rmtree(stage)
    stage.mkdir(parents=True)
    shutil.copy2(src,stage/"TEST_REPORT.json")
    logs=shadow/".opencode-v2/test-logs"
    if logs.is_dir() and not logs.is_symlink(): shutil.copytree(logs,stage/"test-logs",symlinks=False)

def commit_verify_outputs(project: Path, session: str):
    stage=_verify_stage_dir(session); report=stage/"TEST_REPORT.json"
    if not report.is_file(): raise SandboxError(f"no staged Verify outputs for {session}")
    target=project/".opencode-v2/TEST_REPORT.json"
    target.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix="."+target.name+".v2verify.",dir=str(target.parent)); os.close(fd)
    shutil.copy2(report,tmp); os.replace(tmp,target)
    logs=stage/"test-logs"
    if logs.is_dir(): _replace_path(logs,project/".opencode-v2/test-logs")
    return target


def _resolver_snapshot_bwrap_args(run_dir: Path):
    """Preserve resolver configuration while /run remains private.

    Ubuntu commonly makes /etc/resolv.conf a symlink into
    /run/systemd/resolve. V2.6.15 masked all of /run to isolate host systemd,
    DBus and Polkit sockets, which also made that resolver symlink dangling.

    Copy only the resolver file contents into the command's private run
    directory and bind that regular snapshot at the symlink target inside the
    private /run. No host /run directory or socket is re-exposed.
    """
    resolv=Path("/etc/resolv.conf")
    try:
        payload=resolv.read_bytes()
    except OSError as exc:
        raise SandboxError(f"cannot snapshot host resolver configuration: {exc}") from exc

    target=Path(os.path.realpath(str(resolv)))
    try:
        rel=target.relative_to("/run")
    except ValueError:
        # Resolver lives outside /run, so the read-only root bind already
        # preserves it and masking /run cannot break it.
        return []

    if not rel.parts or target.name in ("", ".", ".."):
        raise SandboxError(f"unsafe resolver target under /run: {target}")

    snapshot=run_dir/"resolv.conf.snapshot"
    snapshot.write_bytes(payload)
    snapshot.chmod(0o644)

    args=[]
    current=Path("/run")
    for part in rel.parts[:-1]:
        current=current/part
        args.extend(["--dir",str(current)])
    args.extend(["--ro-bind",str(snapshot),str(target)])
    return args


def run_bash(project: Path, ctx, command: str):
    if not shutil.which("bwrap"):
        raise SandboxError("bubblewrap (bwrap) is required for implementation-worker shell isolation")
    declared=owned_paths(ctx)
    session=ctx["session"]
    _mark_runtime_state(project,ctx)
    base=SANDBOX_ROOT/"runs"/_safe_session_token(session)
    base.mkdir(parents=True,exist_ok=True)
    run_dir=Path(tempfile.mkdtemp(prefix="cmd-",dir=base))
    lower_alias=run_dir/"lower"
    lower_alias.mkdir(parents=True,exist_ok=False)
    shadow=run_dir/"project"
    lower_root=str(lower_alias.resolve())
    build_shadow(project,shadow,declared,lower_root)
    shadow_guards=snapshot_unowned_shadow_guards(shadow,declared,lower_root)

    # lower_alias exists on the host before the root is made read-only. That
    # gives bubblewrap a valid mount target without needing to mkdir anything
    # under the read-only namespace root.
    resolver_args=_resolver_snapshot_bwrap_args(run_dir)
    args=[
        "bwrap","--die-with-parent","--new-session",
        "--unshare-pid","--unshare-ipc",
        "--ro-bind","/","/",
        "--proc","/proc",
        "--dev-bind","/dev","/dev",
        "--tmpfs","/run",
    ]
    args.extend(resolver_args)
    args.extend([
        "--unsetenv","DBUS_SYSTEM_BUS_ADDRESS",
        "--unsetenv","DBUS_SESSION_BUS_ADDRESS",
        "--unsetenv","XDG_RUNTIME_DIR",
        # Mount session-private persistent /tmp before project/shadow binds so
        # later binds still win when a test project happens to live under /tmp.
        "--bind",str(_session_tmpdir(session)),"/tmp",
        "--ro-bind",str(project.resolve()),lower_root,
        "--bind",str(shadow),str(project.resolve()),
    ])
    for kind,src,dst in _ephemeral_mounts(project,session,lower_root):
        args.extend(["--bind",str(src),str(dst)])
    home_scratch=_ephemeral_scratch(session)/"home"
    for rel in (".cache",".npm"):
        host=home_scratch/rel
        host.mkdir(parents=True,exist_ok=True)
        target=Path.home()/rel
        target.mkdir(parents=True,exist_ok=True)
        args.extend(["--bind",str(host),str(target)])
    args.extend(["--tmpfs","/var/tmp"])
    args.extend([
        "--chdir",str(project.resolve()),
        "--setenv","V2_WORKER_SANDBOX","1",
        "--setenv","PYTHONDONTWRITEBYTECODE","1",
        "/bin/bash","-lc",command,
    ])

    started=time.monotonic()
    proc=subprocess.run(args,text=True)
    elapsed=time.monotonic()-started

    violations=detect_unowned_shadow_changes(shadow,declared,shadow_guards)
    merged=merge_owned(project,shadow,declared)

    if violations:
        record_violation(project,ctx,"sandbox-outside-ownership",{
            "paths":violations[:64],
            "command":command[:1000],
            "exit_code":proc.returncode,
        })
        print(
            "WORKER_SANDBOX_DENY out-of-scope project writes were discarded: "
            + ", ".join(violations[:8]),
            file=sys.stderr,
        )
        return 73

    return proc.returncode


def _prepare_verify_shadow(project: Path, shadow: Path, lower_root: str):
    # Verify gets a fully disposable writable snapshot of ordinary project
    # files. Legitimate test/build commands often create outputs outside the
    # known ephemeral directories, so a read-only project view would create
    # another false-negative class. Nothing in this snapshot is merged back.
    #
    # Keep .git read-only via the lower tree. Known ephemeral runtime paths are
    # over-mounted from this exact worker session's preserved scratch state.
    snapshot_paths=[]
    try:
        children=list(project.iterdir())
    except OSError as exc:
        raise SandboxError(f"cannot enumerate project for Verify snapshot: {exc}") from exc
    for child in children:
        rel=child.name
        # Control/query files are published atomically by the live supervisor.
        # Keep the entire control tree on the read-only lower mount instead of
        # copying it into the disposable validator shadow; copying can race a
        # short-lived .<name>.<random>.tmp publication file.
        if rel in {".git", ".opencode-v2"} or path_is_ephemeral(rel):
            continue
        if child.is_dir() and not child.is_symlink():
            rel += "/"
        snapshot_paths.append(rel)
    build_shadow(project,shadow,snapshot_paths,lower_root,enforce_no_symlink=False)

    for rel in EPHEMERAL_DIRS:
        path=shadow/rel
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            path.unlink()
        path.mkdir(parents=True,exist_ok=True)
    for rel in EPHEMERAL_FILES:
        path=shadow/rel
        if path.is_symlink() or (path.exists() and path.is_dir()):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        path.parent.mkdir(parents=True,exist_ok=True)
        path.touch(exist_ok=True)
    return shadow


def run_verify_bash(project: Path, session: str, command: str, agent: str="", timeout=240, stage_test_report=False):
    """Run supervisor Verify with the worker's preserved ephemeral runtime."""
    if not shutil.which("bwrap"):
        raise SandboxError("bubblewrap (bwrap) is required for supervisor verification isolation")
    state=session_runtime_state(session)
    if not state.get("used_bash"):
        raise SandboxError(f"worker sandbox runtime marker missing for {session}")
    if state.get("project") != str(project.resolve()):
        raise SandboxError(f"worker sandbox runtime project mismatch for {session}")
    scratch=SANDBOX_ROOT/"scratch"/_safe_session_token(session)
    if not scratch.is_dir():
        raise SandboxError(f"worker sandbox scratch missing for {session}")

    ctx=resolve_worker(project,session,"",agent)
    if not ctx.get("worker"):
        raise SandboxError(f"verification session {session} is not a current implementation worker")

    base=SANDBOX_ROOT/"runs"/_safe_session_token(session)
    base.mkdir(parents=True,exist_ok=True)
    run_dir=Path(tempfile.mkdtemp(prefix="verify-",dir=base))
    lower_alias=run_dir/"lower"
    lower_alias.mkdir(parents=True,exist_ok=False)
    lower_root=str(lower_alias.resolve())
    shadow=run_dir/"project"
    _prepare_verify_shadow(project,shadow,lower_root)

    resolver_args=_resolver_snapshot_bwrap_args(run_dir)
    args=[
        "bwrap","--die-with-parent","--new-session",
        "--unshare-pid","--unshare-ipc",
        "--ro-bind","/","/",
        "--proc","/proc",
        "--dev-bind","/dev","/dev",
        "--tmpfs","/run",
    ]
    args.extend(resolver_args)
    args.extend([
        "--unsetenv","DBUS_SYSTEM_BUS_ADDRESS",
        "--unsetenv","DBUS_SESSION_BUS_ADDRESS",
        "--unsetenv","XDG_RUNTIME_DIR",
        "--bind",str(_session_tmpdir(session)),"/tmp",
        "--ro-bind",str(project.resolve()),lower_root,
        "--bind",str(shadow),str(project.resolve()),
    ])
    for kind,src,dst in _ephemeral_mounts(project,session,lower_root):
        args.extend(["--bind",str(src),str(dst)])
    home_scratch=_ephemeral_scratch(session)/"home"
    for rel in (".cache",".npm"):
        host=home_scratch/rel
        host.mkdir(parents=True,exist_ok=True)
        target=Path.home()/rel
        target.mkdir(parents=True,exist_ok=True)
        args.extend(["--bind",str(host),str(target)])
    args.extend(["--tmpfs","/var/tmp"])
    args.extend([
        "--chdir",str(project.resolve()),
        "--setenv","V2_WORKER_SANDBOX","verify",
        "--setenv","PYTHONDONTWRITEBYTECODE","1",
        "/bin/bash","-euo","pipefail","-c",command,
    ])

    proc=subprocess.run(args,text=True,timeout=timeout)
    if proc.returncode==0 and stage_test_report:
        stage_verify_outputs(shadow,session)
    # Verify writes are allowed inside this disposable snapshot. They never
    # merge back. Supervisor fingerprints before/after Verify still protect
    # the real project against any unexpected sandbox escape.
    return proc,[]


def _copy_validator_browser_evidence(project: Path, shadow: Path, started_epoch: float):
    evidence=shadow/".opencode-v2/browser-evidence.json"
    if not evidence.is_file() or evidence.is_symlink():
        raise SandboxError("acceptance-browser command produced no regular browser-evidence.json")
    try: data=json.loads(evidence.read_text())
    except Exception as exc: raise SandboxError(f"browser evidence JSON invalid: {exc}") from exc
    raw=str(data.get("generated_at") or "")
    try:
        from datetime import datetime
        ts=datetime.fromisoformat(raw.replace("Z","+00:00")).timestamp()
    except Exception as exc:
        raise SandboxError("browser evidence generated_at is invalid") from exc
    if ts < started_epoch-5:
        raise SandboxError("browser evidence is stale")
    if not isinstance(data.get("url"),str) or not data.get("url"):
        raise SandboxError("browser evidence URL missing")
    ctrl=project/".opencode-v2"; ctrl.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=".browser-evidence.",suffix=".tmp",dir=str(ctrl)); os.close(fd)
    shutil.copy2(evidence,tmp); os.replace(tmp,ctrl/"browser-evidence.json")
    def copy_shot(rel):
        if not isinstance(rel,str) or not rel or rel.startswith(("/","~","./")) or ".." in PurePosixPath(rel).parts:
            raise SandboxError(f"unsafe browser evidence screenshot path: {rel!r}")
        if not rel.startswith(".opencode-v2/acceptance/"):
            raise SandboxError(f"browser evidence screenshot outside acceptance directory: {rel!r}")
        src=shadow/rel
        if not src.is_file() or src.is_symlink():
            raise SandboxError(f"browser evidence screenshot missing/non-regular: {rel}")
        dst=project/rel; dst.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(src,dst)
    if data.get("screenshot"): copy_shot(data.get("screenshot"))
    for item in data.get("canvas_evidence") or []:
        rel=item.get("screenshot") if isinstance(item,dict) else None
        if rel: copy_shot(rel)

def validator_session_root(session: str) -> Path:
    """Return a private host-writable root for nested validator snapshots.

    Acceptance-validator tool calls can run with the user's home mounted
    read-only. The trusted wrapper therefore prepares its disposable snapshot
    under the host temporary directory, before entering the inner bubblewrap.
    """
    root = VALIDATOR_TMP_ROOT
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise SandboxError(f"validator temp root is unsafe: {root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stat = root.stat()
    if stat.st_uid != os.getuid():
        raise SandboxError(f"validator temp root is not owned by uid {os.getuid()}: {root}")
    os.chmod(root, 0o700)
    base = root / _safe_session_token(session)
    if base.exists() and (base.is_symlink() or not base.is_dir()):
        raise SandboxError(f"validator session root is unsafe: {base}")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(base, 0o700)
    return base


def run_validator_bash(project: Path, session: str, command: str, timeout=240):
    """Run acceptance-validator shell in a disposable project snapshot.

    All ordinary writes are discarded. If the trusted acceptance-browser helper
    ran successfully, only its timestamped evidence/screenshots are copied back.
    """
    if not shutil.which("bwrap"):
        raise SandboxError("bubblewrap (bwrap) is required for acceptance-validator shell isolation")
    base=validator_session_root(session)
    run_dir=Path(tempfile.mkdtemp(prefix="cmd-",dir=base))
    lower_alias=run_dir/"lower"; lower_alias.mkdir()
    lower_root=str(lower_alias.resolve()); shadow=run_dir/"project"
    _prepare_verify_shadow(project,shadow,lower_root)
    resolver_args=_resolver_snapshot_bwrap_args(run_dir)
    args=["bwrap","--die-with-parent","--new-session","--unshare-pid","--unshare-ipc","--ro-bind","/","/","--proc","/proc","--dev-bind","/dev","/dev","--tmpfs","/run"]
    args.extend(resolver_args)
    args.extend(["--unsetenv","DBUS_SYSTEM_BUS_ADDRESS","--unsetenv","DBUS_SESSION_BUS_ADDRESS","--unsetenv","XDG_RUNTIME_DIR","--tmpfs","/tmp","--ro-bind",str(project.resolve()),lower_root,"--bind",str(shadow),str(project.resolve()),"--tmpfs","/var/tmp","--chdir",str(project.resolve()),"--setenv","V2_ACCEPTANCE_SANDBOX","1","/bin/bash","-euo","pipefail","-c",command])
    started=time.time()
    try:
        proc=subprocess.run(args,text=True,timeout=timeout)
        if proc.returncode==0 and "acceptance-browser.mjs" in command:
            _copy_validator_browser_evidence(project,shadow,started)
        return proc.returncode
    finally:
        # Remove every disposable validator snapshot on success, command
        # failure, and timeout; trusted evidence has already been copied out.
        shutil.rmtree(run_dir,ignore_errors=True)

def replacement_validator_command(project: Path, session: str, command: str):
    encoded=base64.b64encode(command.encode()).decode()
    parts=["python3",str(Path(__file__).resolve()),"run-validator-bash","--project",str(project.resolve()),"--session",session,"--command-b64",encoded]
    return " ".join(shell_quote(str(part)) for part in parts)


def shell_quote(value: str):
    return "'" + value.replace("'","'\"'\"'") + "'"


def replacement_command(project: Path, ctx, command: str):
    encoded=base64.b64encode(command.encode()).decode()
    parts=[
        "python3",str(Path(__file__).resolve()),"run-bash",
        "--project",str(project.resolve()),
        "--session",ctx["session"],
        "--agent",ctx.get("agent",""),
        "--command-b64",encoded,
    ]
    return " ".join(shell_quote(str(part)) for part in parts)


def normalize_worker_bash_command(project: Path, ctx, command: str):
    """Collapse model-reflected canonical run-bash wrappers back to one layer.

    OpenCode persists tool arguments after the plugin's before-hook rewrite.
    A worker can therefore see the canonical worker_sandbox.py run-bash wrapper
    in its own previous tool history and imitate it on a later bash call.
    Re-wrapping that reflected wrapper nests shell quoting and can corrupt the
    inner command. Only the exact canonical argv for this project/session/agent
    is unwrapped; the decoded payload still executes through run_bash().
    """
    current=str(command)
    prefix=[
        "python3",str(Path(__file__).resolve()),"run-bash",
        "--project",str(project.resolve()),
        "--session",ctx["session"],
        "--agent",ctx.get("agent",""),
        "--command-b64",
    ]
    for _ in range(8):
        try:
            parts=shlex.split(current,posix=True)
        except ValueError:
            return current
        if len(parts)!=len(prefix)+1 or parts[:-1]!=prefix:
            return current
        token=parts[-1]
        try:
            raw=base64.b64decode(token,validate=True)
            current=raw.decode("utf-8")
        except Exception as exc:
            raise SandboxError(
                "WORKER_FIREWALL_DENY malformed canonical sandbox wrapper"
            ) from exc

    try:
        parts=shlex.split(current,posix=True)
    except ValueError:
        return current
    if len(parts)==len(prefix)+1 and parts[:-1]==prefix:
        raise SandboxError(
            "WORKER_FIREWALL_DENY excessive canonical sandbox wrapper nesting"
        )
    return current


def command_invokes_manual_sandbox_wrapper(command: str):
    """Detect a worker trying to invoke sandbox machinery itself."""
    text=str(command or "")
    try:
        parts=shlex.split(text,posix=True)
    except ValueError:
        lowered=text.lower()
        return bool(
            ("worker_sandbox.py" in lowered and "run-bash" in lowered)
            or re.search(r"(^|[;&|()\s])(?:bwrap|bubblewrap)(?=$|[;&|()\s])",lowered)
        )

    lowered=[str(part).lower() for part in parts]
    for index,part in enumerate(parts):
        base=Path(part).name.lower()
        if base in {"bwrap","bubblewrap"}:
            return True
        if base!="worker_sandbox.py":
            continue
        prev=Path(parts[index-1]).name.lower() if index>0 else ""
        following=lowered[index+1:index+5]
        if index==0 or prev.startswith("python") or "run-bash" in following:
            return True
    return False


def normalize_validator_bash_command(project: Path, session: str, command: str):
    """Collapse reflected canonical validator wrappers back to one layer."""
    current=str(command)
    prefix=[
        "python3",str(Path(__file__).resolve()),"run-validator-bash",
        "--project",str(project.resolve()),
        "--session",session,
        "--command-b64",
    ]
    for _ in range(8):
        try:
            parts=shlex.split(current,posix=True)
        except ValueError:
            return current
        if len(parts)!=len(prefix)+1 or parts[:-1]!=prefix:
            return current
        token=parts[-1]
        try:
            raw=base64.b64decode(token,validate=True)
            current=raw.decode("utf-8")
        except Exception as exc:
            raise SandboxError(
                "ACCEPTANCE_FIREWALL_DENY malformed canonical validator wrapper"
            ) from exc
    try:
        parts=shlex.split(current,posix=True)
    except ValueError:
        return current
    if len(parts)==len(prefix)+1 and parts[:-1]==prefix:
        raise SandboxError(
            "ACCEPTANCE_FIREWALL_DENY excessive canonical validator wrapper nesting"
        )
    return current


def hook_guard(project: Path, session: str, call_id: str, agent: str, tool: str, args):
    ctx=resolve_worker(project,session,call_id,agent)
    if not ctx.get("worker"):
        if ctx.get("reason")=="session-unresolved":
            raise SandboxError(
                f"WORKER_FIREWALL_CONTEXT_UNKNOWN cannot identify session for {tool}"
            )
        if ctx.get("agent")=="acceptance-validator":
            if tool=="execute":
                raise SandboxError("ACCEPTANCE_FIREWALL_DENY execute/CodeMode is forbidden")
            if tool in EDIT_TOOLS:
                paths=mutation_paths(tool,args)
                normalized=[_normalize_rel(project,x) for x in paths]
                if normalized != [".opencode-v2/acceptance-report.json"]:
                    raise SandboxError(
                        "ACCEPTANCE_FIREWALL_DENY direct edits are limited to .opencode-v2/acceptance-report.json"
                    )
                assert_no_symlink_components(project,normalized[0])
                return {"action":"pass","worker":False,"validator":True}
            if tool in {"bash","shell"}:
                command=args.get("command") if isinstance(args,dict) else None
                if not isinstance(command,str) or not command.strip():
                    raise SandboxError("acceptance-validator bash command missing")
                command=normalize_validator_bash_command(project,ctx["session"],command)
                return {"action":"replace-validator-bash","worker":False,"validator":True,"command":replacement_validator_command(project,ctx["session"],command)}
        return {"action":"pass","worker":False,"reason":ctx.get("reason","")}
    if tool=="execute":
        record_violation(project,ctx,"forbidden-code-mode",{"tool":tool})
        raise SandboxError(f"WORKER_FIREWALL_DENY {ctx['did']} execute/CodeMode is forbidden")
    if tool in EDIT_TOOLS:
        paths=mutation_paths(tool,args)
        authorize_paths(project,ctx,paths,tool)
        return {"action":"pass","worker":True,"did":ctx["did"]}
    if tool in {"bash","shell"}:
        command=args.get("command") if isinstance(args,dict) else None
        if not isinstance(command,str) or not command.strip():
            raise SandboxError("bash command missing")
        command=normalize_worker_bash_command(project,ctx,command)
        if command_invokes_manual_sandbox_wrapper(command):
            record_violation(
                project,ctx,"manual-sandbox-wrapper",
                {"tool":tool,"command":command[:2000]},
            )
            raise SandboxError(
                f"WORKER_FIREWALL_DENY {ctx['did']} manual sandbox wrapper forbidden; "
                "call bash with only the intended shell command"
            )
        return {
            "action":"replace-bash",
            "worker":True,
            "did":ctx["did"],
            "command":replacement_command(project,ctx,command),
        }
    return {"action":"pass","worker":True,"did":ctx["did"]}


def cleanup_session(session: str):
    token=_safe_session_token(session)
    for area in ("runs","scratch","validator"):
        path=SANDBOX_ROOT/area/token
        if path.exists():
            shutil.rmtree(path,ignore_errors=True)
    validator_tmp = VALIDATOR_TMP_ROOT / token
    if validator_tmp.exists() and not validator_tmp.is_symlink():
        shutil.rmtree(validator_tmp, ignore_errors=True)
    _runtime_state_path(session).unlink(missing_ok=True)
    stage=_verify_stage_dir(session)
    if stage.exists(): shutil.rmtree(stage,ignore_errors=True)


def selftest(require_bwrap=False):
    # Exercise the production mount shape: keep the test project outside /tmp.
    selftest_root=SANDBOX_ROOT/"selftest"
    selftest_root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="v2-sandbox-selftest-",dir=selftest_root) as td:
        project=Path(td)/"project"
        (project/".opencode-v2/work").mkdir(parents=True)

        # Regression: Phase-0/Phase-0.5 control sessions run before attempts.json
        # and the implementation guard manifest exist. The implementation
        # firewall must not block their writes.
        ctx0=resolve_worker(project,"ses_acceptance","","acceptance-planner")
        assert not ctx0["worker"] and ctx0["reason"]=="non-implementation-role", ctx0
        ctx0_unknown=resolve_worker(project,"ses_control_unknown","","")
        assert (
            not ctx0_unknown["worker"]
            and ctx0_unknown["reason"]=="pre-implementation-control-phase"
        ), ctx0_unknown

        # Conversely, once implementation state begins, a missing canonical
        # ledger must still fail closed for an implementation role.
        _atomic_json(project/".opencode-v2/IMPLEMENTATION_PLAN.guard.json",{
            "leaves":{"D001":{"role":"implementer","owned_artifact_paths":["src/owned.txt"]}}
        })
        try:
            resolve_worker(project,"ses_impl_missing_ledger","","implementer")
            raise AssertionError("implementation mutation passed without attempts ledger")
        except SandboxError as exc:
            assert "attempt ledger missing" in str(exc), exc
        (project/".opencode-v2/IMPLEMENTATION_PLAN.guard.json").unlink()

        (project/"src").mkdir()
        (project/"src/owned.txt").write_text("old\n")
        (project/"other.txt").write_text("safe\n")
        manifest={
            "leaves":{
                "D001":{
                    "role":"implementer",
                    "owned_artifact_paths":["src/owned.txt"],
                }
            }
        }
        _atomic_json(project/".opencode-v2/IMPLEMENTATION_PLAN.guard.json",manifest)
        _atomic_json(project/".opencode-v2/work/attempts.json",{
            "owner":"supervisor",
            "deliverables":{"D001":{"count":1,"sessions":["ses_test"]}},
        })
        ctx=resolve_worker(project,"ses_test","","implementer")
        assert ctx["worker"] and ctx["did"]=="D001"

        # Regression: OpenCode persists the plugin-rewritten bash command.
        # If the model mirrors that canonical wrapper, collapse it back to the
        # raw payload before producing exactly one fresh wrapper.
        raw_cmd="printf 'wrapped-ok\\n' > src/owned.txt"
        wrapped=replacement_command(project,ctx,raw_cmd)
        double_wrapped=replacement_command(project,ctx,wrapped)
        assert normalize_worker_bash_command(project,ctx,raw_cmd)==raw_cmd
        assert normalize_worker_bash_command(project,ctx,wrapped)==raw_cmd
        assert normalize_worker_bash_command(project,ctx,double_wrapped)==raw_cmd

        validator_raw="python3 -m unittest discover -s tests -v"
        validator_wrapped=replacement_validator_command(
            project,"ses_acceptance",validator_raw
        )
        validator_double=replacement_validator_command(
            project,"ses_acceptance",validator_wrapped
        )
        assert normalize_validator_bash_command(
            project,"ses_acceptance",validator_raw
        )==validator_raw
        assert normalize_validator_bash_command(
            project,"ses_acceptance",validator_wrapped
        )==validator_raw
        assert normalize_validator_bash_command(
            project,"ses_acceptance",validator_double
        )==validator_raw

        tampered=wrapped+" ; printf bad > other.txt"
        assert normalize_worker_bash_command(project,ctx,tampered)==tampered

        malformed_prefix=[
            "python3",str(Path(__file__).resolve()),"run-bash",
            "--project",str(project.resolve()),
            "--session",ctx["session"],
            "--agent",ctx.get("agent",""),
            "--command-b64","not_base64!",
        ]
        malformed=" ".join(shell_quote(str(part)) for part in malformed_prefix)
        try:
            normalize_worker_bash_command(project,ctx,malformed)
            raise AssertionError("malformed canonical wrapper was accepted")
        except SandboxError as exc:
            assert "malformed canonical sandbox wrapper" in str(exc), exc

        authorize_paths(project,ctx,["src/owned.txt"],"edit")
        try:
            authorize_paths(project,ctx,["other.txt"],"edit")
            raise AssertionError("unowned edit was allowed")
        except SandboxError:
            pass

        # Canonical ownership roots use path-prefix containment consistently
        # with supervisor._path_inside_any(), even without a trailing slash.
        assert path_is_owned(
            "reference/fixtures/epoch-0001.json",
            ["reference/fixtures"],
        )
        assert not path_is_owned(
            "reference/fixtures-sibling/epoch-0001.json",
            ["reference/fixtures"],
        )

        (project/"alias-target").mkdir()
        (project/"alias").symlink_to(project/"alias-target",target_is_directory=True)
        try:
            assert_no_symlink_components(project,"alias/file.txt")
            raise AssertionError("symlink ownership alias was allowed")
        except SandboxError:
            pass

        # Verify snapshots the complete project and must preserve ordinary
        # project symlinks without treating them as worker-owned mutation paths.
        verify_shadow=Path(td)/"verify-symlink-shadow"
        _prepare_verify_shadow(project,verify_shadow,"/v2-lower")
        assert (verify_shadow/"alias").is_symlink(), "Verify snapshot lost ordinary project symlink"

        declared=owned_paths(ctx)
        shadow=Path(td)/"shadow"
        build_shadow(project,shadow,declared)
        assert (shadow/"src/owned.txt").is_file() and not (shadow/"src/owned.txt").is_symlink()
        assert (shadow/"other.txt").is_symlink()
        # Capture immutable command-start guards before simulating worker and
        # concurrent supervisor mutations.
        shadow_guards=snapshot_unowned_shadow_guards(shadow,declared)
        (shadow/"src/owned.txt").write_text("new\n")
        (shadow/"other.txt").unlink()
        (shadow/"other.txt").write_text("bad\n")
        # Concurrent host changes must not become worker violations.
        (project/".opencode-v2/work/supervisor-concurrent.json").write_text("{}\n")
        assert "other.txt" in detect_unowned_shadow_changes(shadow,declared,shadow_guards)
        assert ".opencode-v2/work/supervisor-concurrent.json" not in detect_unowned_shadow_changes(
            shadow,declared,shadow_guards
        )
        merge_owned(project,shadow,declared)
        assert (project/"src/owned.txt").read_text()=="new\n"
        assert (project/"other.txt").read_text()=="safe\n"

        if require_bwrap:
            if not shutil.which("bwrap"):
                raise AssertionError("bwrap is not installed")
            # Clear the deliberate direct-tool violation before the integration run.
            vp=violation_path(project,"D001","ses_test")
            vp.unlink(missing_ok=True)
            rc=run_bash(
                project,ctx,
                "set -e; printf 'shell-owned\\n' > src/owned.txt; "
                "rm -f other.txt; printf 'shell-bad\\n' > other.txt"
            )
            assert rc==73, rc
            assert (project/"src/owned.txt").read_text()=="shell-owned\n"
            assert (project/"other.txt").read_text()=="safe\n"
            vp.unlink(missing_ok=True)

            # New26 regression: supervisor/control-plane files created in the
            # live shared project while a worker shell is running must not be
            # attributed to that worker. The command writes a marker into its
            # session-persistent ephemeral node_modules tree after the shadow
            # snapshot exists; the host thread waits for that marker before
            # committing synthetic supervisor state.
            race_marker=(
                SANDBOX_ROOT/"scratch"/_safe_session_token("ses_test")/
                "node_modules/v2-batch9-race/started"
            )
            concurrent_control=project/".opencode-v2/work/D999.split-transaction.json"
            race_errors=[]
            def host_control_commit():
                deadline=time.monotonic()+5.0
                while time.monotonic()<deadline and not race_marker.exists():
                    time.sleep(0.01)
                if not race_marker.exists():
                    race_errors.append("worker race marker was not observed")
                    return
                concurrent_control.write_text('{"owner":"supervisor"}\n')
            race_thread=threading.Thread(target=host_control_commit,daemon=True)
            race_thread.start()
            rc=run_bash(
                project,ctx,
                "mkdir -p node_modules/v2-batch9-race; "
                "touch node_modules/v2-batch9-race/started; "
                "sleep 0.25; printf 'race-owned\n' > src/owned.txt"
            )
            race_thread.join(timeout=6.0)
            assert not race_thread.is_alive(), "concurrent control thread did not finish"
            assert race_errors==[], race_errors
            assert concurrent_control.is_file(), concurrent_control
            assert rc==0, rc
            assert (project/"src/owned.txt").read_text()=="race-owned\n"
            assert not vp.exists(), vp

            rc=run_bash(
                project,ctx,
                "mkdir -p node_modules/v2-batch8-probe; "
                "printf 'ephemeral-ok\\n' > node_modules/v2-batch8-probe/marker; "
                "printf 'shell-ok\\n' > src/owned.txt"
            )
            assert rc==0, rc
            assert (project/"src/owned.txt").read_text()=="shell-ok\n"
            assert not (project/"node_modules").exists()
            assert session_used_sandbox("ses_test")

            # New25 regression: supervisor Verify must still see the exact
            # worker's ephemeral dependency/runtime state after child idle.
            checked,mutations=run_verify_bash(
                project,"ses_test",
                "test -f node_modules/v2-batch8-probe/marker && "
                "grep -q '^shell-ok$' src/owned.txt"
            )
            assert checked.returncode==0, checked.returncode
            assert mutations==[], mutations

            # Legitimate Verify/build writes are allowed only inside the
            # disposable snapshot and must never change the host project.
            checked,mutations=run_verify_bash(
                project,"ses_test",
                "printf 'verify-disposable\\n' > src/owned.txt; "
                "printf 'temporary\\n' > verify-only.tmp"
            )
            assert checked.returncode==0, checked.returncode
            assert mutations==[], mutations
            assert (project/"src/owned.txt").read_text()=="shell-ok\n"
            assert not (project/"verify-only.tmp").exists()

            checked,mutations=run_verify_bash(
                project,"ses_test",
                "mkdir -p .opencode-v2/test-logs; "
                "printf '{\"status\":\"pass\",\"checks_run\":1}' > .opencode-v2/TEST_REPORT.json; "
                "printf 'ok\n' > .opencode-v2/test-logs/01.log",
                stage_test_report=True,
            )
            assert checked.returncode==0 and mutations==[], (checked.returncode,mutations)
            assert not (project/".opencode-v2/TEST_REPORT.json").exists()
            commit_verify_outputs(project,"ses_test")
            assert (project/".opencode-v2/TEST_REPORT.json").is_file()
            (project/".opencode-v2/TEST_REPORT.json").unlink()

            # Batch 14A: /tmp persists across shell calls for this session,
            # is visible to preserved-environment Verify, and remains isolated
            # from a different session.
            rc=run_bash(
                project,ctx,
                "mkdir -p /tmp/v2-persist && printf 'persist-ok\n' > /tmp/v2-persist/marker"
            )
            assert rc==0, rc
            rc=run_bash(
                project,ctx,
                "grep -q '^persist-ok$' /tmp/v2-persist/marker"
            )
            assert rc==0, "worker /tmp did not persist across shell calls"
            checked,mutations=run_verify_bash(
                project,"ses_test",
                "grep -q '^persist-ok$' /tmp/v2-persist/marker"
            )
            assert checked.returncode==0, "Verify did not reuse session /tmp"
            assert mutations==[], mutations
            other=dict(ctx); other["session"]="ses_other"
            rc=run_bash(
                project,other,
                "test ! -e /tmp/v2-persist/marker"
            )
            assert rc==0, "session-private /tmp leaked into another worker"
            cleanup_session("ses_other")

            # V2.6.16 RESOLVER REGRESSION: masking host /run must not
            # break Ubuntu's /etc/resolv.conf -> /run/systemd/resolve/... link.
            resolver_sha=hashlib.sha256(Path("/etc/resolv.conf").read_bytes()).hexdigest()
            rc=run_bash(
                project,ctx,
                "test -r /etc/resolv.conf && "
                f"test \"$(sha256sum /etc/resolv.conf | awk '{{print $1}}')\" = \"{resolver_sha}\""
            )
            assert rc==0, "worker resolver snapshot missing or changed"
            checked,mutations=run_verify_bash(
                project,"ses_test",
                "test -r /etc/resolv.conf && "
                f"test \"$(sha256sum /etc/resolv.conf | awk '{{print $1}}')\" = \"{resolver_sha}\""
            )
            assert checked.returncode==0, "Verify resolver snapshot missing or changed"
            assert mutations==[], mutations

            # If the host resolver can currently resolve a stable public name,
            # the isolated worker/Verify namespaces must be able to resolve it
            # too. Skip only when the host itself is offline/unresolved so the
            # selftest does not turn an external outage into a harness failure.
            host_dns_ok=subprocess.run(
                ["getent","hosts","example.com"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            ).returncode==0
            if host_dns_ok:
                rc=run_bash(
                    project,ctx,
                    "getent hosts example.com >/dev/null 2>&1"
                )
                assert rc==0, "worker DNS failed while host DNS works"
                checked,mutations=run_verify_bash(
                    project,"ses_test",
                    "getent hosts example.com >/dev/null 2>&1"
                )
                assert checked.returncode==0, "Verify DNS failed while host DNS works"
                assert mutations==[], mutations

            # V2.6.15 HOST-IPC-ISOLATION REGRESSION: worker and Verify
            # sandboxes must not inherit the host systemd/system-bus sockets.
            rc=run_bash(
                project,ctx,
                "test ! -S /run/systemd/private && "
                "test ! -S /run/dbus/system_bus_socket && "
                "test -z \"${DBUS_SYSTEM_BUS_ADDRESS-}\" && "
                "test -z \"${DBUS_SESSION_BUS_ADDRESS-}\""
            )
            assert rc==0, rc
            if shutil.which("systemctl"):
                rc=run_bash(
                    project,ctx,
                    "systemctl --no-ask-password daemon-reload >/dev/null 2>&1"
                )
                assert rc!=0, "systemctl unexpectedly reached a system manager"
            checked,mutations=run_verify_bash(
                project,"ses_test",
                "test ! -S /run/systemd/private && "
                "test ! -S /run/dbus/system_bus_socket && "
                "test -z \"${DBUS_SYSTEM_BUS_ADDRESS-}\" && "
                "test -z \"${DBUS_SESSION_BUS_ADDRESS-}\""
            )
            assert checked.returncode==0, checked.returncode
            assert mutations==[], mutations

            validator_root=validator_session_root("ses_validator_test")
            assert validator_root.parent==VALIDATOR_TMP_ROOT, validator_root
            assert str(validator_root).startswith(tempfile.gettempdir()+os.sep), validator_root
            rc=run_validator_bash(
                project,"ses_validator_test",
                "test -r src/owned.txt && test -r .opencode-v2/IMPLEMENTATION_PLAN.guard.json"
            )
            assert rc==0, f"validator temp-root integration failed rc={rc}"
            cleanup_session("ses_validator_test")
            assert not validator_root.exists(), validator_root

            cleanup_session("ses_test")
            assert not session_used_sandbox("ses_test")
            assert not (SANDBOX_ROOT/"scratch"/_safe_session_token("ses_test")).exists()

    print("worker-sandbox selftest: OK")


def main():
    ap=argparse.ArgumentParser()
    sub=ap.add_subparsers(dest="cmd",required=True)

    h=sub.add_parser("hook")
    h.add_argument("--project",required=True)
    h.add_argument("--session",default="")
    h.add_argument("--call-id",default="")
    h.add_argument("--agent",default="")
    h.add_argument("--tool",required=True)
    h.add_argument("--args-b64",required=True)

    b=sub.add_parser("run-bash")
    b.add_argument("--project",required=True)
    b.add_argument("--session",required=True)
    b.add_argument("--agent",default="")
    b.add_argument("--command-b64",required=True)

    v=sub.add_parser("run-validator-bash")
    v.add_argument("--project",required=True)
    v.add_argument("--session",required=True)
    v.add_argument("--command-b64",required=True)

    c=sub.add_parser("cleanup")
    c.add_argument("--session",required=True)

    st=sub.add_parser("selftest")
    st.add_argument("--require-bwrap",action="store_true")

    ns=ap.parse_args()

    if ns.cmd=="selftest":
        selftest(ns.require_bwrap)
        return 0
    if ns.cmd=="cleanup":
        cleanup_session(ns.session)
        return 0

    project=Path(ns.project).resolve()
    if ns.cmd=="hook":
        try:
            args=json.loads(base64.b64decode(ns.args_b64).decode())
            result=hook_guard(project,ns.session,ns.call_id,ns.agent,ns.tool,args)
            print(json.dumps(result,separators=(",",":")))
            return 0
        except SandboxError as exc:
            print(str(exc),file=sys.stderr)
            return 73

    if ns.cmd=="run-bash":
        try:
            command=base64.b64decode(ns.command_b64).decode()
            ctx=resolve_worker(project,ns.session,"",ns.agent)
            if not ctx.get("worker"):
                raise SandboxError("run-bash session is not a current implementation worker")
            return run_bash(project,ctx,command)
        except SandboxError as exc:
            print(str(exc),file=sys.stderr)
            return 73
    if ns.cmd=="run-validator-bash":
        try:
            command=base64.b64decode(ns.command_b64).decode()
            return run_validator_bash(project,ns.session,command)
        except SandboxError as exc:
            print(str(exc),file=sys.stderr)
            return 73
    return 2


if __name__=="__main__":
    raise SystemExit(main())
