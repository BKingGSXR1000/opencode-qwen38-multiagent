#!/usr/bin/env python3
"""Read-only projection of the V2 filesystem control plane.

This module deliberately derives every value from the authoritative artifacts.
It does not write readiness, attempts, or test state.
"""
import json
from pathlib import Path
from state_io import StateCorruptionError, load_json_object


AUTOMATIC_ATTEMPT_LIMIT = 3
RECURSIVE_SPLIT_PROTOCOL = "v2-recursive-split-v1"
MAX_SPLIT_DEPTH = 2
# A bounded, supervisor-recorded OpenCode failure can reserve one additional
# *dispatch slot* without relabelling a broken beta compaction as a successful
# implementation attempt.  It is deliberately not a general retry mechanism.
MAX_INFRASTRUCTURE_RETRY_GRANTS = 3
# A human authorization may survive one *proven, pre-execution* runtime abort.
# It releases an existing reservation; it never creates a human grant.
MAX_OPERATOR_INFRASTRUCTURE_ABORTS = 1
SPLIT_STATUS_SUFFIX = ".split-status.json"
LEAF_READY_PROTOCOL = "v2-leaf-ready-v1"


# Bootstrap owns this incomplete plan artifact.  Keeping the text here lets the
# project bootstrapper and supervisor identify it without independent templates
# drifting apart.  It deliberately has neither deliverables nor the completion
# marker, so it can never satisfy the deterministic plan guard.
IMPLEMENTATION_PLAN_SCAFFOLD = """# Implementation Plan
Status: INCOMPLETE

## Planner checkpoint
Status: BOOTSTRAP

## Deliverables

## Execution Waves
"""


def _kv(path):
    try:
        return {
            key.strip(): value.strip()
            for line in path.read_text(errors="replace").splitlines()
            if "=" in line
            for key, value in [line.split("=", 1)]
        }
    except OSError:
        return {}


def _base_ready(project, did):
    project = Path(project)
    data = _kv(project / ".opencode-v2" / "work" / f"{did}.ready")
    if not (
        data.get("status") == "complete"
        and data.get("deliverable") == did
        and data.get("verified") == "true"
        and data.get("owner") == "supervisor"
        and data.get("protocol") == LEAF_READY_PROTOCOL
    ):
        return {}
    try:
        ready_attempt = int(data.get("attempt") or 0)
    except (TypeError, ValueError):
        return {}
    ledger = load_attempts(project)
    if ledger.get("owner") != "supervisor":
        return {}
    entry = (ledger.get("deliverables") or {}).get(did)
    state = attempt_state(entry)
    if not state.get("valid") or ready_attempt < 1 or ready_attempt != state.get("count"):
        return {}
    return data


def split_depth(did):
    """Return the deterministic depth for a root or recursively split ID."""
    if not isinstance(did, str):
        return -1
    if __import__("re").fullmatch(r"D\d{3}", did):
        return 0
    if __import__("re").fullmatch(r"D\d{3}-[AB]", did):
        return 1
    if __import__("re").fullmatch(r"D\d{3}-[AB][12]", did):
        return 2
    return -1


def valid_deliverable_id(did):
    return split_depth(did) >= 0


def ready_info(project, did, _seen=None):
    """A split parent is ready only after its children and original check pass."""
    if not _base_ready(project, did):
        return {}
    manifest = load_manifest(project)
    leaf = (manifest.get("leaves") or {}).get(did, {})
    children = leaf.get("split_children", []) if isinstance(leaf, dict) else []
    if not children:
        return _base_ready(project, did)
    seen = set() if _seen is None else set(_seen)
    if did in seen or not isinstance(children, list) or len(children) != 2:
        return {}
    seen.add(did)
    return _base_ready(project, did) if all(ready_info(project, child, seen) for child in children) else {}


def phase_ready(project, name, artifact, marker):
    data = _kv(Path(project) / ".opencode-v2" / name)
    return bool(
        data.get("status") == "complete"
        and data.get("artifact") == artifact
        and data.get("marker") == marker
        and data.get("validated", "").startswith("deterministic-")
    )


def load_manifest(project):
    return load_json_object(
        Path(project) / ".opencode-v2" / "IMPLEMENTATION_PLAN.guard.json",
        default_missing={},
        label="implementation manifest",
    )


def load_attempts(project):
    return load_json_object(
        Path(project) / ".opencode-v2" / "work" / "attempts.json",
        default_missing={"deliverables": {}},
        label="attempt ledger",
    )


def split_status(project, did):
    """Return the supervisor's finite state for one pending split request."""
    return load_json_object(
        Path(project) / ".opencode-v2" / "work" / f"{did}{SPLIT_STATUS_SUFFIX}",
        default_missing={},
        label=f"split status {did}",
    )


def _attempt_state_v2612_original(entry):
    """Validate and project one supervisor-owned attempt ledger entry.

    Counts above the automatic limit are valid only when every excess attempt
    is covered by a recorded, bounded grant.  Human operator grants are the
    normal explicit override.  A single supervisor-recorded OpenCode
    compaction failure that happened before any owned/progress artifact was
    created may reserve one separately auditable recovery slot.  Old ledgers
    without grant fields retain their three-attempt automatic semantics.
    """
    entry = entry if isinstance(entry, dict) else {}
    try:
        count = int(entry.get("count") or 0)
        automatic_limit = int(entry.get("automatic_limit", AUTOMATIC_ATTEMPT_LIMIT))
        operator_grants = int(entry.get("operator_retry_grants") or 0)
        infrastructure_grants = int(entry.get("infrastructure_retry_grants") or 0)
    except (TypeError, ValueError):
        return {"valid": False, "count": -1, "automatic_limit": AUTOMATIC_ATTEMPT_LIMIT,
                "operator_retry_grants": 0, "operator_grants_remaining": 0,
                "operator_grants_used": 0, "operator_grants_reserved": 0,
                "operator_infrastructure_aborted": 0, "operator_infrastructure_blocked": 0,
                "total_dispatches": -1, "automatic_attempts_consumed": 0,
                "infrastructure_retry_grants": 0,
                "infrastructure_grants_remaining": 0,
                "allowed_attempts": AUTOMATIC_ATTEMPT_LIMIT,
                "infrastructure_authorized_attempt": False,
                "operator_authorized_attempt": False}
    overrides = entry.get("operator_overrides", [])
    override_grants = 0
    if overrides:
        if not isinstance(overrides, list):
            overrides = None
        else:
            for override in overrides:
                if not isinstance(override, dict) or override.get("source") != "operator-cli":
                    overrides = None; break
                try:
                    grant = int(override.get("grant"))
                except (TypeError, ValueError):
                    overrides = None; break
                if grant != 1 or not override.get("timestamp") or not override.get("reason"):
                    overrides = None; break
                override_grants += grant
    infrastructure_failures = entry.get("infrastructure_failures", [])
    failure_grants = 0
    if infrastructure_failures:
        if not isinstance(infrastructure_failures, list):
            infrastructure_failures = None
        else:
            for failure in infrastructure_failures:
                if not isinstance(failure, dict):
                    infrastructure_failures = None; break
                if (failure.get("source") != "supervisor" or
                        failure.get("kind") not in {
                            "opencode-compaction-template", "runtime-cancel",
                            "supervisor-compaction-retire", "child-binding-failure",
                        }):
                    infrastructure_failures = None; break
                if not failure.get("timestamp") or not isinstance(failure.get("session"), str) or not failure["session"]:
                    infrastructure_failures = None; break
                if failure.get("evidence") != "no-owned-artifact-or-progress":
                    infrastructure_failures = None; break
                try:
                    grant = int(failure.get("grant"))
                except (TypeError, ValueError):
                    infrastructure_failures = None; break
                if grant != 1:
                    infrastructure_failures = None; break
                failure_grants += grant
    operator_attempts = entry.get("operator_retry_attempts", [])
    if not isinstance(operator_attempts, list):
        operator_attempts = None
    valid_operator_attempts = True
    consumed_operator_attempts = reserved_operator_attempts = 0
    aborted_operator_attempts = blocked_operator_attempts = 0
    seen_operator_sequences = set()
    if operator_attempts is not None:
        for item in operator_attempts:
            if not isinstance(item, dict):
                valid_operator_attempts = False; break
            try:
                sequence = int(item.get("sequence"))
            except (TypeError, ValueError):
                valid_operator_attempts = False; break
            status = item.get("state")
            if (sequence <= automatic_limit or sequence in seen_operator_sequences or
                    not isinstance(item.get("session"), str) or not item["session"] or
                    item.get("source") != "supervisor" or
                    status not in {"reserved", "consumed", "infrastructure_abort", "infrastructure_blocked"}):
                valid_operator_attempts = False; break
            seen_operator_sequences.add(sequence)
            if status == "consumed":
                if item.get("consumes_operator_grant") is not True:
                    valid_operator_attempts = False; break
                consumed_operator_attempts += 1
            elif status == "reserved":
                if item.get("consumes_operator_grant") is not False:
                    valid_operator_attempts = False; break
                reserved_operator_attempts += 1
            elif status == "infrastructure_abort":
                if item.get("consumes_operator_grant") is not False or not item.get("outcome"):
                    valid_operator_attempts = False; break
                aborted_operator_attempts += 1
            else:
                # A second immediate runtime abort is historical but blocks
                # automatic recovery until a human explicitly grants again.
                if item.get("consumes_operator_grant") is not False or not item.get("outcome"):
                    valid_operator_attempts = False; break
                blocked_operator_attempts += 1

    # Existing ledgers have no per-attempt records. Their excess claims retain
    # the old meaning. In a new ledger, only excess not represented by a record
    # is legacy consumption, so an infrastructure-aborted dispatch remains
    # truthful without spending another human authorization.
    operator_dispatches = max(0, count - automatic_limit - infrastructure_grants)
    record_count = len(operator_attempts) if operator_attempts is not None else 0
    legacy_operator_used = max(0, operator_dispatches - record_count)
    operator_used = legacy_operator_used + consumed_operator_attempts
    operator_remaining = operator_grants - operator_used - reserved_operator_attempts - blocked_operator_attempts
    allowed = automatic_limit + operator_grants + infrastructure_grants + aborted_operator_attempts
    valid = (
        count >= 0
        and automatic_limit in (2, AUTOMATIC_ATTEMPT_LIMIT)
        and operator_grants >= 0
        and infrastructure_grants >= 0
        and infrastructure_grants <= MAX_INFRASTRUCTURE_RETRY_GRANTS
        and overrides is not None
        and override_grants == operator_grants
        and infrastructure_failures is not None
        and failure_grants == infrastructure_grants
        and valid_operator_attempts
        and record_count <= operator_dispatches
        and consumed_operator_attempts + reserved_operator_attempts + blocked_operator_attempts <= operator_grants
        and aborted_operator_attempts <= MAX_OPERATOR_INFRASTRUCTURE_ABORTS
        and operator_remaining >= 0
        and count <= allowed
    )
    excess = max(0, count - automatic_limit)
    # Infrastructure credits are consumed first because they are created only
    # for a prior failed dispatch and cannot be created by a model.  This makes
    # the remaining-grant fields deterministic even though the ledger records
    # claims, not a synthetic replacement attempt.
    infrastructure_remaining = max(0, infrastructure_grants - excess)
    return {
        "valid": valid,
        "count": count,
        "automatic_limit": automatic_limit,
        "operator_retry_grants": operator_grants,
        "operator_grants_used": operator_used if valid else 0,
        "operator_grants_reserved": reserved_operator_attempts if valid else 0,
        "operator_grants_remaining": operator_remaining if valid else 0,
        "operator_infrastructure_aborted": aborted_operator_attempts if valid else 0,
        "operator_infrastructure_blocked": blocked_operator_attempts if valid else 0,
        "total_dispatches": count,
        # `count` is the immutable dispatch history.  A bounded infrastructure
        # credit represents one dispatch that never became a real autonomous
        # implementation attempt, so derive the latter rather than rewriting
        # history.
        "automatic_attempts_consumed": max(0, min(count - infrastructure_grants, automatic_limit)) if valid else 0,
        "infrastructure_retry_grants": infrastructure_grants,
        "infrastructure_grants_remaining": infrastructure_remaining if valid else 0,
        "allowed_attempts": allowed,
        "infrastructure_authorized_attempt": valid and excess > 0 and excess <= infrastructure_grants,
        "operator_authorized_attempt": valid and operator_dispatches > 0,
    }

# V2.6.12 INFRASTRUCTURE LEDGER REPAIR BEGIN
def _v2612_repair_infrastructure_attempt_state(entry, state):
    """Repair only the proven New17 infrastructure-grant inconsistency.

    The original attempt_state remains authoritative.  This helper may turn an
    invalid result into a valid one only when every auditable invariant below
    proves that the sole inconsistency is a supervisor-granted infrastructure
    recovery.  Human/operator retry accounting is intentionally excluded.
    """
    if not isinstance(entry, dict) or not isinstance(state, dict):
        return state
    if state.get("valid"):
        return state

    try:
        count = int(entry.get("count") or 0)
        automatic_limit = int(entry.get("automatic_limit") or AUTOMATIC_ATTEMPT_LIMIT)
        infra_grants = int(entry.get("infrastructure_retry_grants") or 0)
    except (TypeError, ValueError):
        return state

    max_infra = int(globals().get("MAX_INFRASTRUCTURE_RETRY_GRANTS", 0) or 0)
    if count < 1 or automatic_limit < 1 or infra_grants < 1:
        return state
    if max_infra and infra_grants > max_infra:
        return state

    # Never use this repair to bypass human/operator accounting.
    if int(entry.get("operator_retry_grants") or 0) != 0:
        return state
    if entry.get("operator_retry_attempts") or entry.get("operator_overrides"):
        return state

    sessions = entry.get("sessions")
    if not isinstance(sessions, list) or len(sessions) != count:
        return state
    if not all(isinstance(sid, str) and sid for sid in sessions):
        return state
    if len(set(sessions)) != len(sessions):
        return state

    infra_failures = entry.get("infrastructure_failures") or []
    if not isinstance(infra_failures, list) or len(infra_failures) != infra_grants:
        return state
    granted_sessions = []
    for item in infra_failures:
        if not isinstance(item, dict):
            return state
        if int(item.get("grant") or 0) != 1 or item.get("source") != "supervisor":
            return state
        sid = item.get("session")
        if not isinstance(sid, str) or sid not in sessions or sid in granted_sessions:
            return state
        granted_sessions.append(sid)

    history = entry.get("failure_history") or []
    if not isinstance(history, list) or len(history) > count:
        return state
    classifications = []
    for item in history:
        if not isinstance(item, dict):
            return state
        classification = item.get("classification")
        if classification not in {"genuine", "infrastructure"}:
            return state
        classifications.append(classification)

    infra_history = classifications.count("infrastructure")
    genuine_failures = classifications.count("genuine")
    # record_infrastructure_abort writes the grant immediately before the
    # matching infrastructure failure-history row, so at most one grant may be
    # temporarily ahead of failure_history.
    if infra_history > infra_grants or infra_grants - infra_history > 1:
        return state
    if genuine_failures > automatic_limit:
        return state

    allowed = automatic_limit + infra_grants
    if count > allowed:
        return state

    # At most one current dispatch may be unclassified while it is still live.
    if count - len(history) not in (0, 1):
        return state

    repaired = dict(state)
    repaired.update({
        "valid": True,
        "count": count,
        "automatic_limit": automatic_limit,
        "automatic_attempts_consumed": genuine_failures,
        "infrastructure_retry_grants": infra_grants,
        "allowed_attempts": allowed,
        "infrastructure_grants_remaining": max(0, allowed - count),
        "infrastructure_authorized_attempt": (
            count >= automatic_limit and count < allowed
        ),
        "v2612_infrastructure_repair": True,
    })
    return repaired


def attempt_state(entry):
    state = _attempt_state_v2612_original(entry)
    return _v2612_repair_infrastructure_attempt_state(entry, state)
# V2.6.12 INFRASTRUCTURE LEDGER REPAIR END



def planner_restarts(project):
    data=load_json_object(
        Path(project) / ".opencode-v2" / "work" / "planner-restarts.json",
        default_missing={"count":0},
        label="planner restart ledger",
    )
    try:
        count=int(data.get("count") or 0)
    except (ValueError,TypeError) as exc:
        raise StateCorruptionError("planner restart ledger count is invalid") from exc
    if count < 0:
        raise StateCorruptionError("planner restart ledger count is negative")
    return count


def test_state(project):
    data=load_json_object(
        Path(project) / ".opencode-v2" / "TEST_REPORT.json",
        default_missing={},
        label="test report",
    )
    passed = data.get("status") == "pass" and isinstance(data.get("checks_run"), int) and data["checks_run"] > 0
    return {"complete": passed, "status": data.get("status"), "checks_run": data.get("checks_run", 0)}


def snapshot(project):
    """Return one derived state snapshot suitable for humans, scripts, or UI mirrors."""
    project = Path(project).resolve()
    manifest = load_manifest(project)
    leaves = manifest.get("leaves") if isinstance(manifest.get("leaves"), dict) else {}
    attempts = load_attempts(project).get("deliverables") or {}
    leaf_states = {}
    for did, leaf in sorted(leaves.items()):
        complete = bool(ready_info(project, did))
        entry = attempts.get(did) if isinstance(attempts.get(did), dict) else {}
        attempt = attempt_state(entry)
        count = attempt["count"]
        deps = leaf.get("launch_deps") if isinstance(leaf, dict) else []
        deps = deps if isinstance(deps, list) else []
        missing = [dep for dep in deps if not ready_info(project, dep)]
        children = leaf.get("split_children", []) if isinstance(leaf, dict) else []
        children = children if isinstance(children, list) else []
        history = entry.get("failure_history", []) if isinstance(entry, dict) else []
        genuine_failures = sum(
            1 for item in history if isinstance(item, dict) and item.get("classification") == "genuine"
        )
        split_required = (project / ".opencode-v2" / "work" / f"{did}.split-request.json").exists()
        pending_split = split_status(project, did) if split_required else {}
        split_state = pending_split.get("state", "split-required") if split_required else ""
        leaf_states[did] = {
            "complete": complete,
            "attempts": count,
            "total_dispatches": attempt["total_dispatches"],
            "automatic_attempts_consumed": attempt["automatic_attempts_consumed"],
            "automatic_limit": attempt["automatic_limit"],
            "operator_retry_grants": attempt["operator_retry_grants"],
            "operator_grants_used": attempt["operator_grants_used"],
            "operator_grants_reserved": attempt["operator_grants_reserved"],
            "operator_grants_remaining": attempt["operator_grants_remaining"],
            "operator_infrastructure_aborted": attempt["operator_infrastructure_aborted"],
            "operator_infrastructure_blocked": attempt["operator_infrastructure_blocked"],
            "operator_authorized_attempt": attempt["operator_authorized_attempt"],
            "infrastructure_retry_grants": attempt["infrastructure_retry_grants"],
            "infrastructure_grants_remaining": attempt["infrastructure_grants_remaining"],
            "infrastructure_authorized_attempt": attempt["infrastructure_authorized_attempt"],
            "attempt_ledger_valid": attempt["valid"],
            "allowed_attempts": attempt["allowed_attempts"],
            "attempt_limit_reached": not complete and (not attempt["valid"] or count >= attempt["allowed_attempts"]),
            "launch_deps_missing": missing,
            "split_depth": split_depth(did),
            "split_children": children,
            "genuine_failures": genuine_failures,
            "split_required": split_required,
            "split_state": split_state,
            "split_generation": pending_split.get("generation", 1) if split_required else 0,
            "eligible": not complete and not children and not split_required and attempt["valid"] and count < attempt["allowed_attempts"] and not missing,
        }
    acceptance_complete = phase_ready(
        project, "ACCEPTANCE.ready", "ACCEPTANCE.md", "ACCEPTANCE_COMPLETE"
    )
    plan_complete = phase_ready(
        project,
        "IMPLEMENTATION_PLAN.ready",
        "IMPLEMENTATION_PLAN.md",
        "IMPLEMENTATION_PLAN_COMPLETE",
    )
    planner_failures = planner_restarts(project)
    tests = test_state(project)
    execution_blockers = []
    for did, leaf in leaf_states.items():
        if leaf["complete"] or leaf["split_children"]:
            continue
        if leaf["split_required"] and leaf.get("split_state") not in {"split-validation-failed", "splitter-failed"}:
            continue
        execution_blockers.append({
            "deliverable": did,
            "reason": (leaf.get("split_state") if leaf["split_required"] else
                       "attempt_ledger_invalid" if not leaf["attempt_ledger_valid"] else
                       "execution_blocked_infrastructure" if leaf["operator_infrastructure_blocked"] else
                       "attempt_limit_reached"),
            "attempts": leaf["attempts"],
            "allowed_attempts": leaf["allowed_attempts"],
        })
    state = {
        "protocol": "V2.6.9",
        "project": str(project),
        "acceptance": {"complete": acceptance_complete},
        "plan": {
            "complete": plan_complete,
            "manifest_present": bool(leaves),
            "planner_failures": planner_failures,
            "blocked": not plan_complete and planner_failures >= 3,
        },
        "leaves": leaf_states,
        "execution_blockers": execution_blockers,
        "tests": tests,
        "acceptance_validation": {
            "complete": (project / ".opencode-v2" / "acceptance-pass.json").exists()
        },
    }
    state["resume_phase"] = resume_phase(state)
    return state


def resume_phase(state):
    """Derive the next root action from durable state only."""
    if not state.get("acceptance", {}).get("complete"):
        return "acceptance"
    if state.get("plan", {}).get("blocked"):
        return "implementation-blocked"
    if not state.get("plan", {}).get("complete"):
        return "implementation-plan"
    leaves = state.get("leaves") or {}
    if any(leaf.get("split_required") and leaf.get("split_state") in {"split-required", "splitter-active"}
           for leaf in leaves.values()):
        return "recursive-split"
    if any(not leaf.get("complete") and leaf.get("attempt_limit_reached") for leaf in leaves.values()):
        return "execution-blocked"
    if any(not leaf.get("complete") for leaf in leaves.values()):
        return "execution"
    if not state.get("tests", {}).get("complete"):
        return "final-tests"
    if not state.get("acceptance_validation", {}).get("complete"):
        return "acceptance-validation"
    return "complete"
