#!/usr/bin/env python3
"""Read-only projection of the V2 filesystem control plane.

This module deliberately derives every value from the authoritative artifacts.
It does not write readiness, attempts, or test state.
"""
import hashlib
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
# One additional replacement is permitted only when the supervisor can prove
# that the worker's authoritative context was mechanically truncated/blocked
# and that the resulting attempt was reclassified as infrastructure.
MAX_CONTEXT_DELIVERY_RETRY_GRANTS = 1
MAX_UNMATERIALIZED_DISPATCH_REPLAYS = MAX_INFRASTRUCTURE_RETRY_GRANTS
# V2.6.9 BATCH8 VERIFY-SANDBOX-LIFETIME-V3
# A human authorization may survive one *proven, pre-execution* runtime abort.
# It releases an existing reservation; it never creates a human grant.
MAX_OPERATOR_INFRASTRUCTURE_ABORTS = 1
SPLIT_STATUS_SUFFIX = ".split-status.json"
LEAF_READY_PROTOCOL = "v2-leaf-ready-v1"
VERIFY_WAIT_PROTOCOL = "v2-verify-wait-v1"
PHASE_READY_PROTOCOL = "V2.6.7c"
PHASE_READY_VALIDATOR = "deterministic-v2.6.7b"
SPLIT_PROGRESS_STATES = frozenset({"split-required","splitter-active","split-retryable"})
SPLIT_TERMINAL_STATES = frozenset({
    "split-validation-failed","splitter-failed",
    "split-unavailable-read-only-parent","parent-finalize-failed",
})


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
    base=_base_ready(project, did)
    if not base:
        return {}
    manifest = load_manifest(project)
    leaf = (manifest.get("leaves") or {}).get(did, {})
    command=str(leaf.get("verify_command") or "") if isinstance(leaf,dict) else ""
    if not command:
        return {}
    expected=str(base.get("verify_sha256") or "")
    if expected:
        if hashlib.sha256(command.encode()).hexdigest()!=expected:
            return {}
    else:
        evidence=load_json_object(
            Path(project)/".opencode-v2"/"work"/f"{did}.verify-evidence.json",
            default_missing={},label=f"verify evidence {did}",
        )
        latest=evidence.get("latest") if isinstance(evidence,dict) else {}
        if not (
            isinstance(latest,dict)
            and latest.get("result")=="verified"
            and latest.get("command")==command
        ):
            return {}
    children = leaf.get("split_children", []) if isinstance(leaf, dict) else []
    if not children:
        return base
    seen = set() if _seen is None else set(_seen)
    if did in seen or not isinstance(children, list) or len(children) != 2:
        return {}
    seen.add(did)
    return base if all(ready_info(project, child, seen) for child in children) else {}


def phase_ready(project, name, artifact, marker):
    root=Path(project)/".opencode-v2"
    data=_kv(root/name)
    artifact_path=root/artifact
    digest=str(data.get("artifact_sha256") or "")
    if not (
        data.get("status")=="complete"
        and data.get("protocol")==PHASE_READY_PROTOCOL
        and data.get("artifact")==artifact
        and data.get("marker")==marker
        and data.get("validated")==PHASE_READY_VALIDATOR
        and len(digest)==64
        and all(ch in "0123456789abcdef" for ch in digest)
        and artifact_path.is_file()
    ):
        return False
    try:
        actual=hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    except OSError:
        return False
    return actual==digest


def load_manifest(project):
    return load_json_object(
        Path(project) / ".opencode-v2" / "IMPLEMENTATION_PLAN.guard.json",
        default_missing={},
        label="implementation manifest",
    )


def attempt_history_evidence(project):
    # Return True when Dxxx artifacts prove that an attempt ledger once existed.
    work=Path(project)/".opencode-v2"/"work"
    if not work.is_dir():
        return False
    patterns=(
        "D*.ready",
        "D*.progress.md",
        "D*.ownership-baseline.json",
        "D*.attempt-*.execution-baseline.json",
        "D*.split-request.json",
        "D*.split-proposal.json",
        "D*.split-status.json",
        "D*.split-transaction.json",
        "D*.verify-wait.json",
    )
    if any(any(work.glob(pattern)) for pattern in patterns):
        return True
    violations=work/"sandbox-violations"
    return violations.is_dir() and any(violations.glob("D*.jsonl"))


def load_attempts(project):
    path=Path(project)/".opencode-v2"/"work"/"attempts.json"
    if not path.exists():
        if attempt_history_evidence(project):
            raise StateCorruptionError(
                "attempt ledger missing after durable Dxxx execution history exists"
            )
        return {"deliverables": {}}
    return load_json_object(path,label="attempt ledger")


def split_status(project, did):
    """Return the supervisor's finite state for one pending split request."""
    return load_json_object(
        Path(project) / ".opencode-v2" / "work" / f"{did}{SPLIT_STATUS_SUFFIX}",
        default_missing={},
        label=f"split status {did}",
    )


def _plan_contract_revision_credit_count(entry, count):
    rows=entry.get("plan_contract_revisions") or [] if isinstance(entry,dict) else []
    if not isinstance(rows,list):
        return 0
    seen=set()
    for row in rows:
        if not isinstance(row,dict) or row.get("source")!="supervisor-plan-contract-revision":
            continue
        try:
            attempt=int(row.get("attempt") or 0)
        except (TypeError,ValueError):
            continue
        if 1 <= attempt <= count:
            seen.add((attempt,str(row.get("current_verify_sha256") or "")))
    return len(seen)


def _context_delivery_recovery_credit_count(entry, count):
    if not isinstance(entry,dict):
        return 0
    rows=entry.get("context_delivery_recoveries") or []
    history=entry.get("failure_history") or []
    if not isinstance(rows,list) or not isinstance(history,list):
        return 0
    recovered=set()
    for row in rows:
        if not isinstance(row,dict):
            continue
        try:
            grant=int(row.get("grant") or 0)
            attempt=int(row.get("attempt") or 0)
        except (TypeError,ValueError):
            continue
        if (
            row.get("source")!="supervisor-context-delivery-repair"
            or grant!=1
        ):
            continue
        session=row.get("session")
        if not (1 <= attempt <= count and isinstance(session,str) and session):
            continue
        matched=False
        for item in history:
            if not isinstance(item,dict):
                continue
            try:
                failure_attempt=int(item.get("attempt") or 0)
            except (TypeError,ValueError):
                continue
            if (
                failure_attempt==attempt
                and item.get("classification")=="infrastructure"
                and item.get("reclassified_by")==
                    "runtime-context-delivery-repair"
            ):
                matched=True
                break
        if matched:
            recovered.add(attempt)
    return min(len(recovered),MAX_CONTEXT_DELIVERY_RETRY_GRANTS)


def _parent_contract_repair_credit_count(entry, count):
    if not isinstance(entry,dict):
        return 0
    history=entry.get("failure_history") or []
    resolution=entry.get("parent_contract_repair_resolution")
    approved=resolution.get("reclassified_attempts") if isinstance(resolution,dict) else None
    if not isinstance(history,list) or not isinstance(approved,list):
        return 0
    approved={item for item in approved if isinstance(item,int) and item>0}
    seen=set()
    for item in history:
        if not isinstance(item,dict) or not (
            item.get("classification")=="bad-plan"
            and item.get("reclassified_by")=="runtime-parent-contract-repair"
        ):
            continue
        try:
            attempt=int(item.get("attempt") or 0)
        except (TypeError,ValueError):
            continue
        if 1 <= attempt <= count and attempt in approved:
            seen.add(attempt)
    return len(seen)


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
                "context_delivery_retry_grants": 0,
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
                            "verification-environment",
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
    # These supervisor-only credits replace an already recorded dispatch. They
    # must be projected before validating a later replacement attempt; doing
    # it afterwards incorrectly turns that legal replacement into an invalid
    # operator reservation.
    plan_contract_credits=_plan_contract_revision_credit_count(entry,count)
    context_delivery_credits=_context_delivery_recovery_credit_count(entry,count)
    bad_plan_credits=_parent_contract_repair_credit_count(entry,count)
    non_operator_credits=(
        infrastructure_grants
        + plan_contract_credits
        + context_delivery_credits
        + bad_plan_credits
    )

    operator_attempts = entry.get("operator_retry_attempts", [])
    if not isinstance(operator_attempts, list):
        operator_attempts = None
    valid_operator_attempts = True
    consumed_operator_attempts = reserved_operator_attempts = 0
    aborted_operator_attempts = blocked_operator_attempts = 0
    seen_operator_sequences = set()
    operator_record_count=0
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
                    status not in {"reserved", "consumed", "infrastructure_abort", "infrastructure_blocked", "plan_contract_replacement", "bad_plan_replacement"}):
                valid_operator_attempts = False; break
            seen_operator_sequences.add(sequence)
            # A prior buggy controller could create a reserved record for a
            # plan-revision/repair replacement.  That sequence is covered by
            # the durable supervisor credit, not by human authorization.
            if sequence <= automatic_limit + non_operator_credits:
                continue
            operator_record_count += 1
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
    operator_dispatches = max(0, count - automatic_limit - non_operator_credits)
    record_count = operator_record_count if operator_attempts is not None else 0
    legacy_operator_used = max(0, operator_dispatches - record_count)
    operator_used = legacy_operator_used + consumed_operator_attempts
    operator_remaining = operator_grants - operator_used - reserved_operator_attempts - blocked_operator_attempts
    allowed = automatic_limit + operator_grants + non_operator_credits + aborted_operator_attempts
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
        "automatic_attempts_consumed": max(0, min(count - non_operator_credits, automatic_limit)) if valid else 0,
        "infrastructure_retry_grants": infrastructure_grants,
        "infrastructure_grants_remaining": infrastructure_remaining if valid else 0,
        "allowed_attempts": allowed,
        "infrastructure_authorized_attempt": valid and excess > 0 and excess <= infrastructure_grants,
        "operator_authorized_attempt": valid and operator_dispatches > 0,
        "bad_plan_retry_grants": bad_plan_credits,
        "plan_contract_retry_grants": plan_contract_credits,
        "context_delivery_retry_grants": context_delivery_credits,
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
    if entry.get("operator_overrides"):
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
        if classification not in {"genuine", "infrastructure", "bad-plan"}:
            return state
        classifications.append(classification)

    infrastructure_rows=[
        item for item in history
        if isinstance(item,dict) and item.get("classification")=="infrastructure"
    ]
    context_delivery_history=sum(
        1 for item in infrastructure_rows
        if item.get("reclassified_by")=="runtime-context-delivery-repair"
    )
    infra_history=len(infrastructure_rows)-context_delivery_history
    genuine_failures = classifications.count("genuine")
    bad_plan_history = classifications.count("bad-plan")
    plan_contract_credits = _plan_contract_revision_credit_count(entry,count)
    context_delivery_credits = _context_delivery_recovery_credit_count(entry,count)
    bad_plan_credits = _parent_contract_repair_credit_count(entry,count)
    # record_infrastructure_abort writes the grant immediately before the
    # matching infrastructure failure-history row, so at most one grant may be
    # temporarily ahead of failure_history.
    if infra_history > infra_grants or infra_grants - infra_history > 1:
        return state
    if context_delivery_history != context_delivery_credits:
        return state
    if genuine_failures > (
        automatic_limit
        + plan_contract_credits
        + context_delivery_credits
        + bad_plan_credits
    ):
        return state
    # Every bad-plan row must be backed by the durable supervisor repair
    # resolution; otherwise this compatibility path must stay fail-closed.
    if bad_plan_history != bad_plan_credits:
        return state

    allowed = (
        automatic_limit
        + infra_grants
        + plan_contract_credits
        + context_delivery_credits
        + bad_plan_credits
    )
    if count > allowed:
        return state

    # Historical preclaim code could write a non-consuming operator reservation
    # for a dispatch that was actually covered by supervisor-only replacement
    # credits. Permit only that exact harmless shape; any real human/operator
    # accounting still fails closed above.
    operator_attempts=entry.get("operator_retry_attempts") or []
    if not isinstance(operator_attempts,list):
        return state
    seen_operator_sequences=set()
    for item in operator_attempts:
        if not isinstance(item,dict):
            return state
        try:
            sequence=int(item.get("sequence") or 0)
        except (TypeError,ValueError):
            return state
        session=item.get("session")
        if (
            sequence<=automatic_limit
            or sequence>allowed
            or sequence in seen_operator_sequences
            or item.get("source")!="supervisor"
            or item.get("state")!="reserved"
            or item.get("consumes_operator_grant") is not False
            or not isinstance(session,str)
            or not session
            or sequence>len(sessions)
            or sessions[sequence-1]!=session
        ):
            return state
        seen_operator_sequences.add(sequence)

    # At most one current dispatch may be unclassified while it is still live.
    if count - len(history) not in (0, 1):
        return state

    excess=max(0,count-automatic_limit)
    repaired = dict(state)
    repaired.update({
        "valid": True,
        "count": count,
        "automatic_limit": automatic_limit,
        "automatic_attempts_consumed": max(
            0,
            min(
                count
                - infra_grants
                - plan_contract_credits
                - context_delivery_credits
                - bad_plan_credits,
                automatic_limit,
            ),
        ),
        "infrastructure_retry_grants": infra_grants,
        "plan_contract_retry_grants": plan_contract_credits,
        "context_delivery_retry_grants": context_delivery_credits,
        "bad_plan_retry_grants": bad_plan_credits,
        "allowed_attempts": allowed,
        # Keep the canonical ordering: infrastructure credits are consumed
        # before plan/bad-plan replacement credits.
        "infrastructure_grants_remaining": max(0,infra_grants-excess),
        "infrastructure_authorized_attempt": (
            count > automatic_limit
            and count <= automatic_limit + infra_grants
        ),
        "v2612_infrastructure_repair": True,
    })
    return repaired


def _decorate_unmaterialized_dispatch(entry, state):
    """Project the one legal zero-work dispatch replay directly in canonical state.

    A current `dispatch:<tool-call-id>` placeholder with no failure classification
    represents a reserved worker slot whose child never materialized.  It may be
    replayed a bounded number of times without consuming another implementation
    attempt.  This rule belongs in the authoritative attempt projection so CLI,
    status UI, and supervisor cannot disagree about exhaustion.
    """
    state=dict(state) if isinstance(state,dict) else {}
    reusable=False
    if state.get("valid") and isinstance(entry,dict):
        try:
            count=int(entry.get("count") or 0)
        except (TypeError,ValueError):
            count=0
        sessions=entry.get("sessions")
        history=entry.get("failure_history") or []
        classified=set()
        if isinstance(history,list):
            for item in history:
                if not isinstance(item,dict):
                    continue
                try:
                    classified.add(int(item.get("attempt") or 0))
                except (TypeError,ValueError):
                    pass
        current=(
            sessions[-1]
            if isinstance(sessions,list) and sessions
            and isinstance(sessions[-1],str)
            else ""
        )
        try:
            seq=int(entry.get("unmaterialized_dispatch_sequence") or 0)
            replays=(
                int(entry.get("unmaterialized_dispatch_replays") or 0)
                if seq==count else 0
            )
        except (TypeError,ValueError):
            replays=MAX_UNMATERIALIZED_DISPATCH_REPLAYS
        reusable=bool(
            count>0
            and current.startswith("dispatch:")
            and count not in classified
            and replays < MAX_UNMATERIALIZED_DISPATCH_REPLAYS
        )
    state["unmaterialized_dispatch_reusable"]=reusable
    return state


def _apply_parent_contract_repair_credits(entry, state):
    """Release only the retry slots explicitly invalidated by plan repair."""
    state=dict(state) if isinstance(state,dict) else {}
    state["bad_plan_retry_grants"]=0
    if state.get("valid") and isinstance(entry,dict):
        state["bad_plan_retry_grants"]=_parent_contract_repair_credit_count(
            entry,int(state.get("count") or 0)
        )
    return state


def _apply_plan_contract_revision_credits(entry, state):
    """Project one replacement slot for each supervisor-recorded plan revision."""
    state=dict(state) if isinstance(state,dict) else {}
    state["plan_contract_retry_grants"]=0
    if state.get("valid") and isinstance(entry,dict):
        state["plan_contract_retry_grants"]=_plan_contract_revision_credit_count(
            entry,int(state.get("count") or 0)
        )
    return state


def attempt_state(entry):
    state = _attempt_state_v2612_original(entry)
    state = _v2612_repair_infrastructure_attempt_state(entry, state)
    state = _apply_parent_contract_repair_credits(entry, state)
    state = _apply_plan_contract_revision_credits(entry, state)
    return _decorate_unmaterialized_dispatch(entry, state)
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
    data=load_json_object(Path(project)/".opencode-v2"/"TEST_REPORT.json",default_missing={},label="test report")
    checks=data.get("checks")
    run=data.get("checks_run"); passed_count=data.get("checks_passed"); missing=data.get("missing_required_files")
    complete=bool(
        data.get("status")=="pass" and isinstance(run,int) and not isinstance(run,bool) and run>0
        and isinstance(passed_count,int) and not isinstance(passed_count,bool) and passed_count==run
        and isinstance(missing,list) and not missing and isinstance(checks,list) and len(checks)==run
        and all(isinstance(item,dict) and item.get("exit_code")==0 and item.get("timed_out") is False for item in checks)
    )
    return {"complete":complete,"status":data.get("status"),"checks_run":run or 0,"checks_passed":passed_count or 0}

def acceptance_pass_valid(project):
    root=Path(project)/".opencode-v2"; path=root/"acceptance-pass.json"
    if not path.exists(): return False
    try:
        data=load_json_object(path,label="acceptance pass")
        if data.get("protocol")!="v2-acceptance-pass-v1" or data.get("result")!="PASS": return False
        pairs=(("acceptance_sha256",root/"ACCEPTANCE.md"),("report_sha256",root/"acceptance-report.json"),("test_report_sha256",root/"TEST_REPORT.json"))
        for key,p in pairs:
            if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=data.get(key): return False
        browser_hash=str(data.get("browser_evidence_sha256") or "")
        if browser_hash:
            bp=root/"browser-evidence.json"
            if not bp.is_file() or hashlib.sha256(bp.read_bytes()).hexdigest()!=browser_hash: return False
        return True
    except Exception:
        return False


def verify_wait_info(project,did):
    path=Path(project)/".opencode-v2"/"work"/f"{did}.verify-wait.json"
    if not path.exists():
        return {}
    data=load_json_object(path,label=f"verify wait {did}")
    if (
        data.get("owner")!="supervisor"
        or data.get("protocol")!=VERIFY_WAIT_PROTOCOL
        or data.get("deliverable")!=did
    ):
        raise StateCorruptionError(f"verify wait {did} is invalid")
    return data


def snapshot(project):
    """Return one derived state snapshot suitable for humans, scripts, or UI mirrors."""
    project = Path(project).resolve()
    acceptance_complete=phase_ready(
        project,"ACCEPTANCE.ready","ACCEPTANCE.md","ACCEPTANCE_COMPLETE"
    )
    plan_complete=phase_ready(
        project,"IMPLEMENTATION_PLAN.ready","IMPLEMENTATION_PLAN.md",
        "IMPLEMENTATION_PLAN_COMPLETE",
    )
    manifest=load_manifest(project)
    if plan_complete:
        if (
            manifest.get("protocol")!="V2.6.9"
            or manifest.get("recursive_split_protocol")!=RECURSIVE_SPLIT_PROTOCOL
            or manifest.get("project")!=str(project)
            or not isinstance(manifest.get("leaves"),dict)
            or not manifest.get("leaves")
        ):
            raise StateCorruptionError(
                "implementation plan is READY but guard manifest is missing or invalid"
            )
        leaves=manifest["leaves"]
    else:
        # Never project stale leaf state from an old guard manifest while the
        # current plan artifact is not hash-bound READY.
        leaves={}
    attempts = load_attempts(project).get("deliverables") or {}
    leaf_states = {}
    for did, leaf in sorted(leaves.items()):
        complete = bool(ready_info(project, did))
        entry = attempts.get(did) if isinstance(attempts.get(did), dict) else {}
        attempt = attempt_state(entry)
        count = attempt["count"]
        launch_deps = leaf.get("launch_deps") if isinstance(leaf, dict) else []
        launch_deps = launch_deps if isinstance(launch_deps, list) else []
        contract_deps = leaf.get("contract_deps") if isinstance(leaf, dict) else []
        contract_deps = contract_deps if isinstance(contract_deps, list) else []
        verify_deps = leaf.get("verify_deps") if isinstance(leaf, dict) else []
        verify_deps = verify_deps if isinstance(verify_deps, list) else []
        launch_missing = [dep for dep in launch_deps if not ready_info(project, dep)]
        contract_missing = [dep for dep in contract_deps if not ready_info(project, dep)]
        verify_missing = [dep for dep in verify_deps if not ready_info(project, dep)]
        verify_wait = verify_wait_info(project,did)
        verification_pending = bool(verify_wait)
        children = leaf.get("split_children", []) if isinstance(leaf, dict) else []
        children = children if isinstance(children, list) else []
        history = entry.get("failure_history", []) if isinstance(entry, dict) else []
        genuine_failures = sum(
            1 for item in history if isinstance(item, dict) and item.get("classification") == "genuine"
        )
        request_exists = (project / ".opencode-v2" / "work" / f"{did}.split-request.json").exists()
        split_marker = entry.get("split_required") if isinstance(entry, dict) else None
        pending_split = split_status(project, did)
        split_state = pending_split.get("state", "")
        split_required = bool(
            not children
            and (
                request_exists
                or isinstance(split_marker, dict)
                or split_state in SPLIT_PROGRESS_STATES
                or split_state in SPLIT_TERMINAL_STATES
            )
        )
        if split_required and not split_state:
            split_state = "split-required"
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
            "unmaterialized_dispatch_reusable": attempt["unmaterialized_dispatch_reusable"],
            "allowed_attempts": attempt["allowed_attempts"],
            "attempt_limit_reached": not complete and (not attempt["valid"] or (count >= attempt["allowed_attempts"] and not attempt["unmaterialized_dispatch_reusable"])),
            "launch_deps_missing": launch_missing,
            "contract_deps_missing": contract_missing,
            "verify_deps_missing": verify_missing,
            "verification_pending": verification_pending,
            "verification_wait_session": verify_wait.get("session","") if verification_pending else "",
            "split_depth": split_depth(did),
            "split_children": children,
            "genuine_failures": genuine_failures,
            "split_required": split_required,
            "split_state": split_state,
            "split_generation": pending_split.get("generation", (split_marker or {}).get("generation",1)) if split_required else 0,
            "eligible": (
                not complete and not children and not split_required
                and not verification_pending
                and attempt["valid"]
                and (count < attempt["allowed_attempts"] or attempt["unmaterialized_dispatch_reusable"])
                and not launch_missing and not contract_missing
            ),
        }
    planner_failures = planner_restarts(project)
    tests = test_state(project)
    execution_blockers = []
    for did, leaf in leaf_states.items():
        if leaf["complete"]:
            continue
        if leaf["split_children"]:
            if leaf.get("split_state") != "parent-finalize-failed":
                continue
            execution_blockers.append({
                "deliverable":did,
                "reason":"parent-finalize-failed",
                "attempts":leaf["attempts"],
                "allowed_attempts":leaf["allowed_attempts"],
            })
            continue
        if leaf["split_required"]:
            if leaf.get("split_state") in SPLIT_PROGRESS_STATES:
                continue
            if leaf.get("split_state") in SPLIT_TERMINAL_STATES:
                execution_blockers.append({
                    "deliverable":did,
                    "reason":leaf.get("split_state"),
                    "attempts":leaf["attempts"],
                    "allowed_attempts":leaf["allowed_attempts"],
                })
                continue
        if not leaf["attempt_limit_reached"] and leaf["attempt_ledger_valid"] and not leaf["operator_infrastructure_blocked"]:
            continue
        execution_blockers.append({
            "deliverable": did,
            "reason": ("attempt_ledger_invalid" if not leaf["attempt_ledger_valid"] else
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
            "complete": acceptance_pass_valid(project)
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
    if any(
        leaf.get("split_required") and leaf.get("split_state") in SPLIT_PROGRESS_STATES
        for leaf in leaves.values()
    ):
        return "recursive-split"
    if state.get("execution_blockers"):
        return "execution-blocked"
    if any(not leaf.get("complete") for leaf in leaves.values()):
        return "execution"
    if not state.get("tests", {}).get("complete"):
        return "final-tests"
    if not state.get("acceptance_validation", {}).get("complete"):
        return "acceptance-validation"
    return "complete"
