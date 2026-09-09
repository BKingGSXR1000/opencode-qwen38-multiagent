#!/usr/bin/env python3
"""Audit resolved OpenCode agent permissions from its current HTTP API."""
import argparse
import base64
import json
import os
import sys
import urllib.request


IMPLEMENTATION_AGENTS = {
    "probe-builder", "implementer", "core-builder", "feature-builder",
    "reasoning-builder", "integrator", "tester", "test-builder",
}


def permission_effects(agent, action):
    return {
        item.get("effect")
        for item in agent.get("permissions", [])
        if item.get("action") == action
    }


def audit_agents(payload):
    agents = payload.get("data", payload) if isinstance(payload, dict) else payload
    by_name = {agent.get("name"): agent for agent in agents if isinstance(agent, dict)}
    errors = []
    if permission_effects(by_name.get("orchestrator", {}), "todowrite") != {"allow"}:
        errors.append("orchestrator todowrite must resolve to allow")
    for name in sorted(IMPLEMENTATION_AGENTS):
        if permission_effects(by_name.get(name, {}), "todowrite") != {"deny"}:
            errors.append(f"{name} todowrite must resolve to deny")
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("OPENCODE_AGENT_CONFIG_URL", ""))
    parser.add_argument("--password", default=os.environ.get("OPENCODE_SERVER_PASSWORD", ""))
    parser.add_argument("--resolved-json")
    args = parser.parse_args()
    if args.resolved_json:
        payload = json.load(open(args.resolved_json, encoding="utf-8"))
    else:
        if not args.url or not args.password:
            parser.error("provide --resolved-json or --url and --password")
        request = urllib.request.Request(args.url.rstrip("/") + "/api/agent")
        token = base64.b64encode(f"opencode:{args.password}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.load(response)
    errors = audit_agents(payload)
    if errors:
        print("AGENT_CONFIG_AUDIT_FAIL: " + "; ".join(errors), file=sys.stderr)
        return 2
    print("AGENT_CONFIG_AUDIT_PASS: orchestrator=todowrite:allow workers=todowrite:deny")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
