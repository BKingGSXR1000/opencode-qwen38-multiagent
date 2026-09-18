#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT_DEFAULT = Path.home() / "AI/opencode-qwen38-multiagent-v2"
COMMAND = "v2-native-tool-hook-probe"
CHILD_AGENT = "transport-tool-probe"
CHILD_MARKER = "V2_CHILD_TOOL_HOOK_DONE"
TARGET_REL = ".opencode-v2/transport-tool-target.txt"


def fail(msg: str) -> None:
    raise SystemExit(f"ERROR: {msg}")


class Client:
    def __init__(self, root: Path, project: Path):
        self.root = root
        self.project = project
        self.base = ""
        self.password = ""
        self.pid = 0
        self.port = 0

    def headers(self, json_body=False):
        h = {"Accept": "application/json"}
        if self.password:
            token = base64.b64encode(f"opencode:{self.password}".encode()).decode()
            h["Authorization"] = f"Basic {token}"
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def request(self, method, path, payload=None, timeout=8):
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=body,
            method=method,
            headers=self.headers(payload is not None),
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        if not raw:
            return {}
        obj = json.loads(raw)
        return obj.get("data") if isinstance(obj, dict) and set(obj) == {"data"} else obj

    def discover(self):
        target = os.path.realpath(str(self.root / "xdg/config"))
        try:
            ss = subprocess.check_output(
                ["ss", "-ltnp"], text=True, stderr=subprocess.DEVNULL
            )
        except Exception as exc:
            fail(f"cannot inspect listening ports: {exc}")

        for proc in Path("/proc").glob("[0-9]*"):
            try:
                pid = int(proc.name)
                cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                if "opencode2" not in cmd.lower() or "serve --stdio" not in cmd.lower():
                    continue
                env = {}
                for item in (proc / "environ").read_bytes().split(b"\0"):
                    if b"=" not in item:
                        continue
                    k, v = item.split(b"=", 1)
                    env[k.decode(errors="replace")] = v.decode(errors="replace")
                xdg = env.get("XDG_CONFIG_HOME")
                if xdg and os.path.realpath(xdg) != target:
                    continue
                password = env.get("OPENCODE_PASSWORD") or env.get("OPENCODE_SERVER_PASSWORD")
                if not password:
                    continue
                ports = []
                for line in ss.splitlines():
                    if f"pid={pid}" not in line:
                        continue
                    ports.extend(int(x.group(1)) for x in re.finditer(r"127\.0\.0\.1:(\d+)", line))
                for port in dict.fromkeys(ports):
                    self.base = f"http://127.0.0.1:{port}"
                    self.password = password
                    q = urllib.parse.urlencode({"directory": str(self.project)})
                    try:
                        self.request("GET", f"/api/session/active?{q}", timeout=2)
                        self.pid = pid
                        self.port = port
                        return
                    except Exception:
                        pass
            except Exception:
                continue
        fail("isolated OpenCode2 HTTP server not found; start run-base.sh first")


def db_connect(db: Path):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)


def session_row(db: Path, sid: str):
    con = db_connect(db)
    try:
        return con.execute(
            "SELECT id,parent_id,coalesce(agent,''),coalesce(directory,''),"
            "time_created,time_idle FROM session_v2 WHERE id=?",
            (sid,),
        ).fetchone()
    finally:
        con.close()


def child_rows(db: Path, parent: str):
    con = db_connect(db)
    try:
        return con.execute(
            "SELECT id,parent_id,coalesce(agent,''),coalesce(directory,''),"
            "time_created,time_idle FROM session_v2 "
            "WHERE parent_id=? ORDER BY time_created",
            (parent,),
        ).fetchall()
    finally:
        con.close()


def messages(db: Path, sid: str):
    con = db_connect(db)
    try:
        return con.execute(
            "SELECT seq,type,data FROM session_message WHERE session_id=? ORDER BY seq",
            (sid,),
        ).fetchall()
    finally:
        con.close()


def parse_json(raw):
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def text_from_message(data) -> str:
    if not isinstance(data, dict):
        return ""
    parts = data.get("content")
    if not isinstance(parts, list):
        parts = data.get("parts")
    if not isinstance(parts, list):
        return str(data.get("text") or "")
    out = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            out.append(part["text"])
        state = part.get("state")
        if isinstance(state, dict) and isinstance(state.get("output"), str):
            out.append(state["output"])
    return "\n".join(out)


def token_total(data) -> int:
    if not isinstance(data, dict):
        return 0
    candidates = []
    if isinstance(data.get("tokens"), dict):
        candidates.append(data["tokens"])
    info = data.get("info")
    if isinstance(info, dict) and isinstance(info.get("tokens"), dict):
        candidates.append(info["tokens"])
    total = 0
    for tok in candidates:
        for key in ("input", "output", "reasoning"):
            value = tok.get(key)
            if isinstance(value, (int, float)):
                total += int(value)
        cache = tok.get("cache")
        if isinstance(cache, dict):
            for key in ("read", "write"):
                value = cache.get(key)
                if isinstance(value, (int, float)):
                    total += int(value)
    return total


def root_model_turns(db: Path, sid: str):
    rows = []
    for seq, typ, raw in messages(db, sid):
        if typ != "assistant":
            continue
        data = parse_json(raw)
        t = token_total(data)
        if t > 0:
            rows.append((seq, t, text_from_message(data)[:160]))
    return rows


def child_has_marker(db: Path, sid: str):
    for _, typ, raw in messages(db, sid):
        if typ != "assistant":
            continue
        if CHILD_MARKER in text_from_message(parse_json(raw)):
            return True
    return False


def read_probe_log(project: Path):
    path = project / ".opencode-v2" / "transport-probe.jsonl"
    result = []
    try:
        for line in path.read_text(errors="replace").splitlines():
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                result.append(item)
    except OSError:
        pass
    return result


def delete_session(client: Client, sid: str):
    if not sid:
        return
    q = urllib.parse.urlencode({"directory": str(client.project)})
    try:
        client.request(
            "DELETE",
            f"/api/session/{urllib.parse.quote(sid)}?{q}",
            timeout=8,
        )
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(
        description="Verify that tools called inside a native command child still hit plugin execute hooks."
    )
    ap.add_argument("--project", required=True, type=Path)
    ap.add_argument("--root", type=Path, default=ROOT_DEFAULT)
    ap.add_argument("--timeout", type=float, default=120.0)
    ns = ap.parse_args()

    root_dir = ns.root.expanduser().resolve()
    project = ns.project.expanduser().resolve()
    db = root_dir / "xdg/data/opencode/opencode.db"
    if not db.is_file():
        fail(f"missing OpenCode DB: {db}")

    project.mkdir(parents=True, exist_ok=True)
    control = project / ".opencode-v2"
    control.mkdir(parents=True, exist_ok=True)
    (project / TARGET_REL).write_text("V2_CHILD_TOOL_TARGET_OK\n", encoding="utf-8")
    try:
        (control / "transport-probe.jsonl").unlink()
    except FileNotFoundError:
        pass

    client = Client(root_dir, project)
    client.discover()
    q = urllib.parse.urlencode({"directory": str(project)})

    root_sid = ""
    child_sid = ""
    command_error = ""

    print("A2 child tool-hook transport probe")
    print("==================================")
    print(f"server pid      : {client.pid}")
    print(f"server port     : {client.port}")
    print(f"project         : {project}")
    print(f"command         : /{COMMAND}")
    print()

    try:
        created = client.request(
            "POST",
            f"/api/session?{q}",
            payload={
                "agent": "orchestrator",
                "title": "V2 child tool-hook probe",
                "model": {"id": "qwen38-orchestrator", "providerID": "syv"},
            },
            timeout=8,
        )
        root_sid = created.get("id", "") if isinstance(created, dict) else ""
        if not root_sid:
            fail(f"root create returned no id: {created!r}")

        root = session_row(db, root_sid)
        if not root:
            fail("created root session missing from DB")
        print(f"root session    : {root_sid}")

        payload = {
            "text": f"/{COMMAND}",
            "command": COMMAND,
            "arguments": "",
            "agent": "orchestrator",
            "model": "syv/qwen38-orchestrator",
        }
        try:
            client.request(
                "POST",
                f"/api/session/{urllib.parse.quote(root_sid)}/command?{q}",
                payload=payload,
                timeout=30,
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:2000]
            command_error = f"HTTP {exc.code}: {detail}"
        except Exception as exc:
            command_error = repr(exc)

        deadline = time.monotonic() + ns.timeout
        while time.monotonic() < deadline:
            matches = [r for r in child_rows(db, root_sid) if r[2] == CHILD_AGENT]
            if matches:
                child_sid = matches[-1][0]
                break
            if command_error:
                break
            time.sleep(0.1)

        if not child_sid:
            print(f"command result  : {command_error or 'returned without child'}")
            fail("no transport-tool-probe child was created")

        child = session_row(db, child_sid)
        print(f"child session   : {child_sid}")
        print(f"child parent_id : {child[1]!r}")
        print(f"child agent     : {child[2]}")
        print(f"command result  : {command_error or 'HTTP request completed'}")

        while time.monotonic() < deadline:
            child = session_row(db, child_sid)
            if child and child[5] is not None and child_has_marker(db, child_sid):
                break
            time.sleep(0.2)

        child = session_row(db, child_sid)
        child_completed = bool(child and child[5] is not None)
        child_marker = child_has_marker(db, child_sid)
        time.sleep(2.0)

        logs = read_probe_log(project)
        before = [
            x for x in logs
            if x.get("event") == "child-tool-before"
            and x.get("session") == child_sid
        ]
        after = [
            x for x in logs
            if x.get("event") == "child-tool-after"
            and x.get("session") == child_sid
        ]
        root_turns = root_model_turns(db, root_sid)

        print()
        print(f"PARENTING              : {'PASS' if child[1] == root_sid else 'FAIL'}")
        print(f"CHILD COMPLETED        : {'PASS' if child_completed else 'FAIL'}")
        print(f"CHILD MARKER           : {'PASS' if child_marker else 'FAIL'}")
        print(f"CHILD TOOL BEFORE HOOK : {'PASS' if before else 'FAIL'}")
        print(f"CHILD TOOL AFTER HOOK  : {'PASS' if after else 'FAIL'}")
        print(f"ROOT MODEL TOKEN TURNS : {len(root_turns)}")
        if logs:
            print("PROBE LOG:")
            for item in logs:
                print("  " + json.dumps(item, sort_keys=True))

        if child[1] != root_sid:
            fail("child parenting failed")
        if not child_completed or not child_marker:
            fail("tool-probe child did not complete expected one-read workflow")
        if not before or not after:
            fail("child tool call bypassed plugin execute hook(s)")
        if root_turns:
            fail("root model inference occurred during child tool-hook probe")

        print()
        print("TRANSPORT TOOL-HOOK RESULT: PASS")
        print("Parent TaskTool hooks are bypassed by beta command transport, but child")
        print("tool calls still traverse plugin execute.before/after. This preserves")
        print("worker-side guard hooks if dispatch preclaim/runtime prompt handling is")
        print("moved into the deterministic controller.")
    finally:
        delete_session(client, child_sid)
        delete_session(client, root_sid)


if __name__ == "__main__":
    main()
