#!/usr/bin/env python3
"""Bind a running OpenCode server to the canonical V2 agent/plugin contract."""
import argparse
import hashlib
import json
import os
import tempfile
import time
import urllib.parse
from pathlib import Path

PROTOCOL = "v2-opencode-runtime-contract-v2"


def contract_paths(root: Path):
    root = root.resolve()
    paths = [
        root / "xdg/config/opencode/opencode.jsonc",
        root / "xdg/config/opencode/AGENTS.md",
        root / "xdg/config/opencode/plugins/v2-bounded-subagent.js",
        root / "scripts/stage_a_path_permissions.py",
    ]
    paths.extend(sorted((root / "xdg/config/opencode/agents").glob("*.md")))
    return paths


def overlay_contract_paths(overlay: Path):
    overlay=overlay.resolve()
    opencode=overlay/"opencode"
    paths=[
        opencode/"opencode.jsonc",
        opencode/"AGENTS.md",
        opencode/"plugins/v2-bounded-subagent.js",
        opencode/".stage-a-permission-overlay.json",
    ]
    paths.extend(sorted((opencode/"agents").glob("*.md")))
    return paths


def _paths_sha256(paths, root: Path, label: str):
    root=root.resolve()
    digest=hashlib.sha256()
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"{label} missing/unsafe: {path}")
        data=path.read_bytes()
        rel=path.relative_to(root).as_posix().encode("utf-8")
        digest.update(rel+b"\0")
        digest.update(str(len(data)).encode("ascii")+b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def contract_sha256(root: Path):
    root=root.resolve()
    return _paths_sha256(
        contract_paths(root),root,"runtime contract source"
    )


def overlay_contract_sha256(overlay: Path):
    overlay=overlay.resolve()
    return _paths_sha256(
        overlay_contract_paths(overlay),overlay,"runtime overlay contract"
    )


def process_start_ticks(pid: int):
    raw = Path(f"/proc/{int(pid)}/stat").read_text()
    end = raw.rfind(")")
    if end < 0:
        raise ValueError("invalid /proc stat")
    fields = raw[end + 2 :].split()
    return int(fields[19])


def state_path(root: Path, base_url: str):
    parsed = urllib.parse.urlparse(str(base_url))
    if parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
        raise ValueError(f"unsupported OpenCode base URL: {base_url!r}")
    return root.resolve() / "runtime" / f"a2-server-{parsed.port}.json"


def write_state(root: Path, project: Path, base_url: str, server_pid: int, overlay: Path):
    root = root.resolve(); project = project.resolve(); overlay = overlay.resolve()
    path = state_path(root, base_url)
    payload = {
        "protocol": PROTOCOL,
        "root": str(root),
        "project": str(project),
        "base_url": str(base_url).rstrip("/"),
        "server_pid": int(server_pid),
        "server_start_ticks": process_start_ticks(server_pid),
        "overlay_config_home": str(overlay),
        "canonical_contract_sha256": contract_sha256(root),
        "overlay_contract_sha256": overlay_contract_sha256(overlay),
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return payload


def verify_state(root: Path, project: Path, base_url: str):
    root = root.resolve(); project = project.resolve()
    path = state_path(root, base_url)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"runtime contract marker unavailable: {path}") from exc
    if not isinstance(data, dict) or data.get("protocol") != PROTOCOL:
        raise ValueError("runtime contract marker protocol mismatch")
    expected = {
        "root": str(root),
        "project": str(project),
        "base_url": str(base_url).rstrip("/"),
        "canonical_contract_sha256": contract_sha256(root),
    }
    for key, value in expected.items():
        if data.get(key) != value:
            raise ValueError(f"runtime contract mismatch: {key}")
    try:
        pid = int(data.get("server_pid"))
        ticks = int(data.get("server_start_ticks"))
        current_ticks = process_start_ticks(pid)
    except (TypeError, ValueError, OSError) as exc:
        raise ValueError("runtime contract server process is not live") from exc
    if ticks != current_ticks:
        raise ValueError("runtime contract server process identity changed")
    overlay = Path(str(data.get("overlay_config_home") or ""))
    if not overlay.is_dir():
        raise ValueError("runtime contract overlay is unavailable")
    overlay_sha = overlay_contract_sha256(overlay)
    if data.get("overlay_contract_sha256") != overlay_sha:
        raise ValueError("runtime contract overlay mismatch")
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--server-pid", type=int)
    ap.add_argument("--overlay", type=Path)
    ns = ap.parse_args()
    if ns.write:
        if not ns.server_pid or not ns.overlay:
            ap.error("--write requires --server-pid and --overlay")
        data = write_state(ns.root, ns.project, ns.base_url, ns.server_pid, ns.overlay)
    else:
        data = verify_state(ns.root, ns.project, ns.base_url)
    print(json.dumps(data, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
