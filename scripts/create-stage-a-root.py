#!/usr/bin/env python3
"""Create one fresh, explicitly pinned technical root for a Stage-A project."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from stage_a_controller import ControllerError, http_json, workspace_url


class RootCreateError(RuntimeError):
    pass


ROOT_TITLE = "Stage-A deterministic transport root"


def create_root(project: Path, base_url: str, request=http_json) -> dict:
    project = project.resolve()
    if not project.is_dir():
        raise RootCreateError(f"project does not exist: {project}")
    if not base_url.startswith("http://127.0.0.1:"):
        raise RootCreateError("base URL must be a localhost HTTP URL with an explicit port")
    payload = {
        "title": ROOT_TITLE,
        "agent": "transport-root",
        "model": {"providerID": "v2noop", "modelID": "root-noop"},
    }
    try:
        status, body = request("POST", workspace_url(base_url, "/session", project), payload)
    except ControllerError as exc:
        raise RootCreateError(str(exc)) from exc
    if status not in {200, 201} or not isinstance(body, dict):
        raise RootCreateError(f"transport-root creation failed: status={status} body={body!r}")
    created = body.get("data", body)
    if not isinstance(created, dict):
        raise RootCreateError(f"transport-root creation response is invalid: {body!r}")
    sid = str(created.get("id") or "")
    if not sid:
        raise RootCreateError("transport-root creation response lacks a session id")
    agent = str(created.get("agent") or "")
    if agent and agent != "transport-root":
        raise RootCreateError(f"created root agent is {agent!r}, expected transport-root")
    return {
        "protocol": "v2-stage-a-root-create-v1",
        "project": str(project),
        "root_session": sid,
        "agent": "transport-root",
        "model": "v2noop/root-noop",
        "tracker": "explicit-caller-session",
    }


def selftest() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        seen = {}

        def fake_request(method, url, payload):
            seen.update(method=method, url=url, payload=payload)
            return 201, {"id": "ses-technical-root", "agent": "transport-root"}

        receipt = create_root(project, "http://127.0.0.1:57123", fake_request)
        if receipt["root_session"] != "ses-technical-root":
            raise RootCreateError(f"root receipt mismatch: {receipt!r}")
        if seen.get("method") != "POST" or seen.get("payload", {}).get("model") != {
            "providerID": "v2noop", "modelID": "root-noop"
        }:
            raise RootCreateError(f"root request was not explicitly pinned: {seen!r}")
    print("stage-a root create selftest: OK")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create one fresh technical root after the Stage-A supervisor is running."
    )
    parser.add_argument("--project", type=Path)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--selftest", action="store_true")
    ns = parser.parse_args()
    if ns.selftest:
        selftest()
        return 0
    if ns.project is None or not ns.base_url:
        parser.error("--project and --base-url are required unless --selftest is used")
    print(json.dumps(create_root(ns.project, ns.base_url), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RootCreateError as exc:
        raise SystemExit(f"ERROR: {exc}")
