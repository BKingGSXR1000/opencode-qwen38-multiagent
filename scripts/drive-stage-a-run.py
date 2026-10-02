#!/usr/bin/env python3
"""Quietly drive a generic Stage-A run from its deterministic projection."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import time
from pathlib import Path

import runtime_contract
import stage_a_controller as controller
import stage_a_preflight as preflight


_tick_spec = importlib.util.spec_from_file_location(
    "stage_a_tick", Path(__file__).with_name("run-stage-a-tick.py")
)
if _tick_spec is None or _tick_spec.loader is None:
    raise RuntimeError("Stage-A tick module is unavailable")
tick = importlib.util.module_from_spec(_tick_spec)
_tick_spec.loader.exec_module(tick)


HARNESS_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ENV_KEYS = (
    "V2_OPENCODE_BASE_URL",
    "V2_OPENCODE_DB",
    "V2_ROOT",
    "V2_OPENCODE_SESSION_TABLE",
)


def read_process_environment(pid: int) -> dict[str,str]:
    raw=Path(f"/proc/{int(pid)}/environ").read_bytes()
    result={}
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key,value=item.split(b"=",1)
        try:
            result[key.decode("utf-8")]=value.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return result


def configure_runtime_environment(project: Path, base_url: str) -> dict[str,str]:
    project=project.resolve()
    state=runtime_contract.verify_state(HARNESS_ROOT,project,base_url)
    try:
        pid=int(state["server_pid"])
    except (KeyError,TypeError,ValueError) as exc:
        raise DriverError("runtime marker missing valid server_pid") from exc
    try:
        source=read_process_environment(pid)
    except OSError as exc:
        raise DriverError(f"unable to read verified server environment: {exc}") from exc

    missing=[key for key in RUNTIME_ENV_KEYS if not str(source.get(key) or "").strip()]
    if missing:
        raise DriverError(
            "verified server environment missing required keys: "
            + ",".join(missing)
        )
    expected_base=str(base_url).rstrip("/")
    if str(source["V2_OPENCODE_BASE_URL"]).rstrip("/")!=expected_base:
        raise DriverError("verified server environment base URL mismatch")
    if Path(source["V2_ROOT"]).resolve()!=HARNESS_ROOT:
        raise DriverError("verified server environment root mismatch")
    if str(source["V2_OPENCODE_SESSION_TABLE"])!="session":
        raise DriverError("verified server environment session table mismatch")
    db=Path(source["V2_OPENCODE_DB"])
    if not db.is_file():
        raise DriverError(f"verified server database is unavailable: {db}")

    bound={key:str(source[key]) for key in RUNTIME_ENV_KEYS}
    bound["V2_PROJECT"]=str(project)
    os.environ.update(bound)
    return bound


class DriverError(RuntimeError):
    pass


BLOCKED_STABILITY_OBSERVATIONS = 3
AUTONOMOUS_SPLIT_RECONCILIATION_STATES = frozenset({
    "split-required",
    "splitter-active",
    "split-retryable",
    "split-validation-failed",
})


def blocked_split_reconciliation_pending(project: Path, receipt: dict) -> bool:
    """Do not terminalize an attempt-limit snapshot while split recovery is live."""
    if receipt.get("outcome") != "no-dispatch":
        return False
    actions = receipt.get("actions") if isinstance(receipt.get("actions"), list) else []
    for action in actions:
        if not isinstance(action,dict):
            continue
        if (
            action.get("kind")!="blocked"
            or action.get("reason")!="attempt_limit_reached"
        ):
            continue
        did=str(action.get("deliverable") or "")
        if not did:
            continue
        path=project/".opencode-v2"/"work"/f"{did}.split-status.json"
        try:
            status=json.loads(path.read_text())
        except (OSError,json.JSONDecodeError):
            continue
        if not isinstance(status,dict):
            continue
        if status.get("owner")!="supervisor":
            continue
        if str(status.get("parent_id") or "")!=did:
            continue
        if str(status.get("state") or "") in AUTONOMOUS_SPLIT_RECONCILIATION_STATES:
            return True
    return False


def transient_controller_race(exc: BaseException) -> bool:
    message = str(exc)
    return any(
        marker in message
        for marker in (
            "transport root is not idle",
            "state changed before dispatch:",
            "deterministic actions changed before dispatch",
            "LOGICAL_IMPLEMENTATION_DISPATCH_SETTLING",
            "LOGICAL_TASK_SPLITTER_DISPATCH_SETTLING",
        )
    )


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


def blocked_observation_key(receipt: dict) -> tuple[str, str] | None:
    if receipt.get("outcome") != "no-dispatch":
        return None
    actions = receipt.get("actions") if isinstance(receipt.get("actions"), list) else []
    if terminal(actions) != 2:
        return None
    return (
        str(receipt.get("state_version") or ""),
        json.dumps(actions, sort_keys=True, separators=(",", ":")),
    )


def drive(project: Path, base_url: str, root_session: str, proof: Path, poll: float, max_ticks: int) -> int:
    preflight.verify_proof(proof, project, base_url, root_session)
    dispatched = 0
    last_event = ""
    blocked_key: tuple[str, str] | None = None
    blocked_streak = 0
    while max_ticks == 0 or dispatched < max_ticks:
        try:
            receipt = tick.execute_one(project, base_url, root_session)
        except controller.ControllerError as exc:
            # Runtime state can change between selection and the execution
            # lock while another worker finalizes. These optimistic-concurrency
            # races are reasons to re-project, never reasons to die or replay
            # the stale dispatch.
            if not transient_controller_race(exc):
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
            blocked_key = None
            blocked_streak = 0
        if receipt.get("outcome") == "no-dispatch":
            result = terminal(receipt.get("actions") or [])
            if result == 0:
                return 0
            if result == 2:
                if blocked_split_reconciliation_pending(project,receipt):
                    blocked_key=None
                    blocked_streak=0
                    time.sleep(poll)
                    continue
                current_key = blocked_observation_key(receipt)
                if current_key == blocked_key:
                    blocked_streak += 1
                else:
                    blocked_key = current_key
                    blocked_streak = 1
                if blocked_streak >= BLOCKED_STABILITY_OBSERVATIONS:
                    return 2
            else:
                blocked_key = None
                blocked_streak = 0
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
    blocked_receipt = {
        "outcome": "no-dispatch",
        "state_version": "state-a",
        "actions": [{"kind": "blocked", "reason": "attempt_limit_reached"}],
    }
    if blocked_observation_key(blocked_receipt) != (
        "state-a",
        '[{"kind":"blocked","reason":"attempt_limit_reached"}]',
    ):
        raise DriverError("blocked observation key mismatch")
    if blocked_observation_key({
        "outcome": "no-dispatch",
        "state_version": "state-a",
        "actions": [{"kind": "wait"}],
    }) is not None:
        raise DriverError("wait produced a blocked observation key")
    for message in (
        "transport root is not idle",
        "state changed before dispatch: selected=a current=b",
        "deterministic actions changed before dispatch",
        "LOGICAL_IMPLEMENTATION_DISPATCH_SETTLING deliverable=D001 generation=0",
        "LOGICAL_TASK_SPLITTER_DISPATCH_SETTLING deliverable=D011 generation=1",
    ):
        if not transient_controller_race(controller.ControllerError(message)):
            raise DriverError(f"transient controller race was not recognized: {message}")
    if transient_controller_race(controller.ControllerError("real deterministic failure")):
        raise DriverError("non-transient controller failure was masked")
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
    project=ns.project.resolve()
    configure_runtime_environment(project,ns.base_url)
    return drive(project, ns.base_url, ns.root_session, ns.preflight_proof, ns.poll, ns.max_ticks)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DriverError as exc:
        raise SystemExit(f"ERROR: {exc}")
