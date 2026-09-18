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
COMMAND = "v2-native-transport-probe"
CHILD_AGENT = "transport-probe"
CHILD_MARKER = "V2_NATIVE_TRANSPORT_CHILD_OK"


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
        fail("isolated OpenCode2 HTTP server not found; start run-base.sh for the probe project first")


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


def root_model_token_messages(db: Path, root_sid: str):
    result = []
    for seq, typ, raw in messages(db, root_sid):
        if typ != "assistant":
            continue
        data = parse_json(raw)
        tokens = token_total(data)
        if tokens > 0:
            result.append((seq, tokens, text_from_message(data)[:180]))
    return result


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
        description="A2 probe: command -> native TaskTool -> background child -> root interrupt."
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
    log_path = control / "transport-probe.jsonl"
    try:
        log_path.unlink()
    except FileNotFoundError:
        pass

    client = Client(root_dir, project)
    client.discover()

    q = urllib.parse.urlencode({"directory": str(project)})
    root_sid = ""
    child_sid = ""
    command_error = ""

    print("A2 native deterministic transport probe (subagent_type adapter)")
    print("=======================================")
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
                "title": "V2 deterministic transport probe",
                "model": {"id": "qwen38-orchestrator", "providerID": "syv"},
            },
            timeout=8,
        )
        root_sid = created.get("id", "") if isinstance(created, dict) else ""
        if not root_sid:
            fail(f"root create returned no id: {created!r}")
        row = session_row(db, root_sid)
        if not row:
            fail("created root session missing from DB")
        print(f"root session    : {root_sid}")
        print(f"root parent_id  : {row[1]!r}")
        print(f"root agent      : {row[2]}")

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
                timeout=20,
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
            time.sleep(0.1)

        if not child_sid:
            print(f"command result  : {command_error or 'returned without child'}")
            fail("no native transport-probe child was created")

        child = session_row(db, child_sid)
        print(f"child session   : {child_sid}")
        print(f"child parent_id : {child[1]!r}")
        print(f"child agent     : {child[2]}")
        print(f"child directory : {child[3]}")
        print(f"command result  : {command_error or 'HTTP request completed'}")

        parenting_ok = child[1] == root_sid

        while time.monotonic() < deadline:
            child = session_row(db, child_sid)
            if child and child[5] is not None and child_has_marker(db, child_sid):
                break
            time.sleep(0.2)

        child = session_row(db, child_sid)
        child_idle = bool(child and child[5] is not None)
        marker_ok = child_has_marker(db, child_sid)

        time.sleep(5.0)

        root_token_rows = root_model_token_messages(db, root_sid)
        logs = read_probe_log(project)
        root_request_logs = [
            x for x in logs
            if x.get("event") == "root-model-request" and x.get("session") == root_sid
        ]
        after_logs = [x for x in logs if x.get("event") == "task-after" and x.get("session") == root_sid]
        interrupt_logs = [x for x in logs if x.get("event") == "root-interrupt" and x.get("session") == root_sid]

        print()
        print(f"PARENTING              : {'PASS' if parenting_ok else 'FAIL'}")
        print(f"CHILD COMPLETED        : {'PASS' if child_idle else 'FAIL'}")
        print(f"CHILD MARKER           : {'PASS' if marker_ok else 'FAIL'}")
        print(f"TASK AFTER HOOK        : {'PASS' if after_logs else 'FAIL'}")
        print(f"ROOT INTERRUPT HOOK    : {'PASS' if interrupt_logs else 'FAIL'}")
        print(f"ROOT MODEL TOKEN TURNS : {len(root_token_rows)}")
        print(f"ROOT MODEL HOOK EVENTS : {len(root_request_logs)}")

        if root_token_rows:
            for seq, tokens, preview in root_token_rows:
                print(f"  root token turn seq={seq} tokens={tokens} text={preview!r}")
        if root_request_logs:
            for item in root_request_logs[-5:]:
                print(f"  root model hook: {json.dumps(item, sort_keys=True)}")

        if not parenting_ok:
            fail("native TaskTool child has wrong parent_id")
        if not child_idle or not marker_ok:
            fail("background child did not independently finish with the probe marker")
        if not after_logs:
            fail("probe execute.after hook was not observed")
        if not interrupt_logs:
            fail("root interrupt hook was not observed")

        if root_token_rows or root_request_logs:
            print()
            print("TRANSPORT RESULT: PARTIAL")
            print("Native parenting/background survival works, but OpenCode generated a")
            print("root-model continuation. A2 must suppress or bypass completion injection")
            print("before deterministic execution can replace the LLM scheduler.")
            raise SystemExit(3)

        print()
        print("TRANSPORT RESULT: PASS")
        print("Native child parenting works, the background child survives the parent")
        print("interrupt, and no root-model turn was observed.")
    finally:
        delete_session(client, child_sid)
        delete_session(client, root_sid)


if __name__ == "__main__":
    main()
