#!/usr/bin/env python3
"""Trusted human operator controls for V2 execution-blocked projects.

This command is intentionally outside the OpenCode model tool surface.  Agents
are globally denied shell access to its CLI; only a human invoking it grants an
attempt beyond the autonomous three-attempt limit.
"""
import argparse
import json
import sys
from pathlib import Path

import supervisor
from control_state import load_manifest, load_attempts, ready_info, attempt_state


def exhausted_incomplete(project):
    leaves = (load_manifest(project).get("leaves") or {})
    entries = (load_attempts(project).get("deliverables") or {})
    selected = []
    for did in sorted(leaves):
        if ready_info(project, did):
            continue
        state = attempt_state(entries.get(did))
        if state["valid"] and state["count"] >= state["allowed_attempts"]:
            selected.append(did)
    return selected


def grant(project, dids, reason="explicit operator retry command"):
    project = str(Path(project).resolve())
    previous = supervisor.PROJECT
    supervisor.PROJECT = project
    try:
        return [did for did, _ in supervisor.grant_operator_retry(dids, reason)]
    finally:
        supervisor.PROJECT = previous


def main(argv=None):
    parser = argparse.ArgumentParser(description="Grant one human-authorized retry to exhausted V2 leaves.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--reason", default="explicit operator retry command")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("retry-failed", help="grant every exhausted incomplete planned leaf")
    retry = sub.add_parser("retry", help="grant only named exhausted incomplete leaves")
    retry.add_argument("deliverables", nargs="+")
    args = parser.parse_args(argv)
    project = Path(args.project).resolve()
    if args.command == "retry-failed":
        dids = exhausted_incomplete(project)
        if not dids:
            parser.error("no exhausted incomplete planned leaves")
    else:
        dids = args.deliverables
    try:
        granted = grant(project, dids, args.reason)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({"operator_retry_granted": granted, "project": str(project)}, indent=2))


if __name__ == "__main__":
    main()
