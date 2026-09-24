#!/usr/bin/env python3
"""Quietly drive a generic Stage-A run from its deterministic projection."""
from __future__ import annotations

import argparse
import importlib.util
import json
import time
from pathlib import Path

import stage_a_controller as controller
import stage_a_preflight as preflight


_tick_spec = importlib.util.spec_from_file_location(
    "stage_a_tick", Path(__file__).with_name("run-stage-a-tick.py")
)
if _tick_spec is None or _tick_spec.loader is None:
    raise RuntimeError("Stage-A tick module is unavailable")
tick = importlib.util.module_from_spec(_tick_spec)
_tick_spec.loader.exec_module(tick)


class DriverError(RuntimeError):
    pass


def compact_event(receipt: dict) -> dict:
    if receipt.get("outcome") == "dispatched":
        action = ((receipt.get("receipt") or {}).get("action") or {})
        replay = bool((receipt.get("receipt") or {}).get("replay_suppressed"))
        return {"event": "replay-observed" if replay else "dispatched", "action": action}
    actions = receipt.get("actions") if isinstance(receipt.get("actions"), list) else []
    return {"event": "no-dispatch", "actions": actions}


def terminal(actions: list[object]) -> int | None:
    kinds = {str(item.get("kind") or "") for item in actions if isinstance(item, dict)}
    if "complete" in kinds:
        return 0
    if kinds & {"blocked", "needs_projection"}:
        return 2
    return None


def drive(project: Path, base_url: str, root_session: str, proof: Path, poll: float, max_ticks: int) -> int:
    preflight.verify_proof(proof, project, base_url, root_session)
    dispatched = 0
    last_event = ""
    while max_ticks == 0 or dispatched < max_ticks:
        try:
            receipt = tick.execute_one(project, base_url, root_session)
        except controller.ControllerError as exc:
            # A root can be transiently busy while its child materializes. This
            # is neither a scheduling decision nor a reason to dispatch again.
            if "transport root is not idle" not in str(exc):
                raise DriverError(str(exc)) from exc
            time.sleep(poll)
            continue
        event = compact_event(receipt)
        rendered = json.dumps(event, sort_keys=True)
        if rendered != last_event:
            print(rendered, flush=True)
            last_event = rendered
        if event["event"] == "dispatched":
            dispatched += 1
        if receipt.get("outcome") == "no-dispatch":
            result = terminal(receipt.get("actions") or [])
            if result is not None:
                return result
        time.sleep(poll)
    return 0


def selftest() -> None:
    if compact_event({"outcome": "dispatched", "receipt": {"action": {"agent": "a"}}}) != {
        "event": "dispatched", "action": {"agent": "a"}
    }:
        raise DriverError("dispatch receipt compaction mismatch")
    if terminal([{"kind": "complete"}]) != 0 or terminal([{"kind": "blocked"}]) != 2:
        raise DriverError("terminal state classification mismatch")
    if terminal([{"kind": "wait"}]) is not None:
        raise DriverError("wait was classified terminal")
    print("stage-a driver selftest: OK")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Drive a generic Stage-A run without reading model transcripts."
    )
    parser.add_argument("--project", type=Path)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--root-session", default="")
    parser.add_argument("--preflight-proof", type=Path)
    parser.add_argument("--poll", type=float, default=1.0)
    parser.add_argument("--max-ticks", type=int, default=0)
    parser.add_argument("--selftest", action="store_true")
    ns = parser.parse_args()
    if ns.selftest:
        selftest()
        return 0
    if ns.project is None or not ns.base_url or not ns.root_session or ns.preflight_proof is None:
        parser.error("--project, --base-url, --root-session, and --preflight-proof are required unless --selftest is used")
    if ns.poll <= 0 or ns.max_ticks < 0:
        parser.error("--poll must be > 0 and --max-ticks must be >= 0")
    return drive(ns.project.resolve(), ns.base_url, ns.root_session, ns.preflight_proof, ns.poll, ns.max_ticks)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DriverError as exc:
        raise SystemExit(f"ERROR: {exc}")
