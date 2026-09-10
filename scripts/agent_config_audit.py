#!/usr/bin/env python3
"""Audit resolved OpenCode agent permissions from its current HTTP API."""
import argparse
import base64
import json
import os
import sys
import urllib.request


PLANNER_EDIT_TARGETS = {
    "acceptance-planner": ".opencode-v2/ACCEPTANCE.md",
    "implementation-planner": ".opencode-v2/IMPLEMENTATION_PLAN.md",
}
WRITER_AGENTS = {
    "probe-builder", "implementer", "core-builder", "feature-builder",
    "reasoning-builder", "integrator", "test-builder", "tester", "state-writer",
    "reference-researcher", "acceptance-validator", "lessons-learner",
}
CONTROL_WRAPPER_DENY_AGENTS = {
    "probe-builder", "implementer", "core-builder", "feature-builder",
    "reasoning-builder", "integrator", "test-builder", "tester",
    "acceptance-validator", "lessons-learner", "state-writer",
}


def permission_resources(agent, action, effect):
    return {
        item.get("resource")
        for item in agent.get("permissions", [])
        if item.get("action") == action and item.get("effect") == effect
    }


def audit_agents(payload):
    agents = payload.get("data", payload) if isinstance(payload, dict) else payload
    by_name = {agent.get("name"): agent for agent in agents if isinstance(agent, dict)}
    errors = []
    for name, target in PLANNER_EDIT_TARGETS.items():
        allowed = permission_resources(by_name.get(name, {}), "edit", "allow")
        if allowed != {target}:
            errors.append(f"{name} edit must resolve only to {target}")
    for name in sorted(WRITER_AGENTS):
        if not permission_resources(by_name.get(name, {}), "edit", "allow"):
            errors.append(f"{name} must resolve with edit/write capability")
    for name in sorted(CONTROL_WRAPPER_DENY_AGENTS):
        denied = permission_resources(by_name.get(name, {}), "edit", "deny")
        if ".opencode-v2/bin/*" not in denied:
            errors.append(f"{name} must deny edits to .opencode-v2/bin/*")
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
    print("AGENT_CONFIG_AUDIT_PASS: planners=edit-targeted writers=edit-enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
