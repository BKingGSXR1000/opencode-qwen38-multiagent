#!/usr/bin/env python3
"""Materialize compact read-only scheduler views from the canonical snapshot.

This module never computes scheduler eligibility. It only projects fields that
the supervisor already computed into small files that the root orchestrator can
read without loading complete control-status/guard artifacts into context.
"""
import hashlib
import json
import re
from pathlib import Path

from state_io import atomic_write_text

QUERY_PROTOCOL = "v2-materialized-control-query-v1"
LEAF_CONTEXT_PROTOCOL = "v2-leaf-context-v1"
MAX_DECISION_CHARS = 6000
DID_RE = re.compile(r"^D\d{3}(?:-[AB](?:[12])?)?$")


def _scheduler(raw):
    raw = raw if isinstance(raw, dict) else {}
    return {
        "max_concurrent_workers": int(raw.get("max_concurrent_workers") or 0),
        "active_workers": int(raw.get("active_workers") or 0),
        "reserved_workers": int(raw.get("reserved_workers") or 0),
        "available_worker_slots": int(raw.get("available_worker_slots") or 0),
        "active_deliverables": sorted(raw.get("active_deliverables") or []),
        "error": str(raw.get("error") or ""),
    }


def _blockers(raw):
    result = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        row = {
            "deliverable": str(item.get("deliverable") or ""),
            "reason": str(item.get("reason") or ""),
            "attempts": int(item.get("attempts") or 0),
            "allowed_attempts": int(item.get("allowed_attempts") or 0),
        }
        if item.get("detail"):
            row["detail"] = str(item["detail"])[:500]
        result.append(row)
    return result


def _leaf_projection(did, raw, role=""):
    raw = raw if isinstance(raw, dict) else {}
    result = {
        "protocol": QUERY_PROTOCOL,
        "deliverable": did,
        "role": str(role or ""),
        "complete": bool(raw.get("complete")),
        "eligible": bool(raw.get("eligible")),
        "running": bool(raw.get("running")),
        "attempts": int(raw.get("attempts") or 0),
        "total_dispatches": int(raw.get("total_dispatches") or 0),
        "automatic_attempts_consumed": int(raw.get("automatic_attempts_consumed") or 0),
        "automatic_limit": int(raw.get("automatic_limit") or 0),
        "allowed_attempts": int(raw.get("allowed_attempts") or 0),
        "attempt_limit_reached": bool(raw.get("attempt_limit_reached")),
        "attempt_ledger_valid": raw.get("attempt_ledger_valid") is not False,
        "unmaterialized_dispatch_reusable": bool(raw.get("unmaterialized_dispatch_reusable")),
        "launch_deps_missing": list(raw.get("launch_deps_missing") or []),
        "contract_deps_missing": list(raw.get("contract_deps_missing") or []),
        "verify_deps_missing": list(raw.get("verify_deps_missing") or []),
        "verification_pending": bool(raw.get("verification_pending")),
        "split_required": bool(raw.get("split_required")),
        "split_state": str(raw.get("split_state") or ""),
        "split_generation": int(raw.get("split_generation") or 0),
        "split_children": list(raw.get("split_children") or []),
        "operator_grants_remaining": int(raw.get("operator_grants_remaining") or 0),
        "operator_infrastructure_blocked": int(raw.get("operator_infrastructure_blocked") or 0),
        "infrastructure_grants_remaining": int(raw.get("infrastructure_grants_remaining") or 0),
    }
    reasons = []
    if not result["eligible"]:
        if result["complete"]:
            reasons.append("complete")
        if result["running"]:
            reasons.append("running")
        if result["split_required"]:
            reasons.append(result["split_state"] or "split_required")
        if result["verification_pending"]:
            reasons.append("verification_pending")
        if not result["attempt_ledger_valid"]:
            reasons.append("attempt_ledger_invalid")
        if result["attempt_limit_reached"]:
            reasons.append("attempt_limit_reached")
        if result["operator_infrastructure_blocked"]:
            reasons.append("operator_infrastructure_blocked")
        if result["launch_deps_missing"]:
            reasons.append("launch_deps_missing")
        if result["contract_deps_missing"]:
            reasons.append("contract_deps_missing")
        if not reasons:
            reasons.append("not_currently_eligible")
    result["ineligibility_reasons"] = reasons
    return result



def _acceptance_must_texts(project):
    # Return exact one-line Axxx descriptions from the durable acceptance contract.
    path = Path(project) / ".opencode-v2" / "ACCEPTANCE.md"
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}
    result = {}
    for match in re.finditer(
        r"(?m)^\s*-\s*\[\s*\]\s*(A\d{3})\s*:\s*(.+?)\s*$",
        text,
    ):
        result[match.group(1)] = match.group(2).strip()
    return result


def _as_string_list(raw):
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, (str, int, float))]


def build_leaf_contexts(project, manifest):
    # Project canonical leaf contracts into compact worker-readable packets.
    project = Path(project)
    manifest = manifest if isinstance(manifest, dict) else {}
    leaves = manifest.get("leaves") if isinstance(manifest.get("leaves"), dict) else {}
    acceptance = _acceptance_must_texts(project)
    contexts = {}

    for did, leaf in sorted(leaves.items()):
        if not DID_RE.fullmatch(str(did)) or not isinstance(leaf, dict):
            continue
        if isinstance(leaf.get("split_children"), list) and leaf.get("split_children"):
            # A split parent is not executable; only terminal children need packets.
            continue

        acceptance_ids = _as_string_list(leaf.get("acceptance_ids"))
        packet = {
            "protocol": LEAF_CONTEXT_PROTOCOL,
            "deliverable": did,
            "source_kind": "split-child" if "-" in did else "plan-leaf",
            "name": str(leaf.get("name") or ""),
            "outcome": str(leaf.get("outcome") or leaf.get("name") or ""),
            "role": str(leaf.get("role") or ""),
            "owned_artifacts": leaf.get("owned_artifacts", ""),
            "owned_artifact_paths": _as_string_list(leaf.get("owned_artifact_paths")),
            "launch_deps": _as_string_list(leaf.get("launch_deps")),
            "contract_deps": _as_string_list(leaf.get("contract_deps")),
            "verify_deps": _as_string_list(leaf.get("verify_deps")),
            "acceptance_ids": acceptance_ids,
            "acceptance_musts": [
                {"id": aid, "text": acceptance.get(aid, "")}
                for aid in acceptance_ids
            ],
            "complexity": str(leaf.get("complexity") or ""),
            "repeated_operations": int(leaf.get("repeated_operations") or 0),
            "deep_reasoning": bool(leaf.get("deep_reasoning")),
            "verify_command": str(leaf.get("verify_command") or ""),
            "done_when": str(leaf.get("done_when") or ""),
            "split_handoff_only": bool(leaf.get("split_handoff_only")),
            "split_handoff_source": str(leaf.get("split_handoff_source") or ""),
        }

        if "-" in did:
            scope_path = project / ".opencode-v2" / "work" / f"{did}.scope.md"
            try:
                packet["split_scope"] = scope_path.read_text(errors="replace")
                packet["context_error"] = ""
            except OSError:
                packet["split_scope"] = ""
                packet["context_error"] = "split_scope_missing"
        else:
            packet["context_error"] = ""

        contract_material = json.dumps(packet, sort_keys=True, separators=(",", ":"))
        packet["contract_version"] = hashlib.sha256(
            contract_material.encode("utf-8")
        ).hexdigest()[:16]
        contexts[did] = packet

    return contexts


def build_query_views(snapshot, manifest, source_rendered):
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    leaves = snapshot.get("leaves") if isinstance(snapshot.get("leaves"), dict) else {}
    manifest_leaves = manifest.get("leaves") if isinstance(manifest.get("leaves"), dict) else {}
    roles = {
        did: str(leaf.get("role") or "")
        for did, leaf in manifest_leaves.items()
        if isinstance(leaf, dict)
    }
    version = hashlib.sha256(source_rendered.encode("utf-8")).hexdigest()[:16]
    source_bytes = len(source_rendered.encode("utf-8"))
    base = {
        "protocol": QUERY_PROTOCOL,
        "state_version": version,
        "source_bytes": source_bytes,
        "state_error": bool(snapshot.get("state_error")),
        "resume_phase": str(snapshot.get("resume_phase") or "execution-blocked"),
    }

    eligible = sorted(
        did for did, leaf in leaves.items()
        if isinstance(leaf, dict) and leaf.get("eligible") is True
    )
    eligible_roles = {did: roles.get(did, "") for did in eligible}
    split_required = sorted(
        (
            {
                "deliverable": did,
                "split_state": str(leaf.get("split_state") or "split-required"),
                "split_generation": int(leaf.get("split_generation") or 0),
            }
            for did, leaf in leaves.items()
            if isinstance(leaf, dict) and leaf.get("split_required") is True
        ),
        key=lambda item: item["deliverable"],
    )
    blockers = _blockers(snapshot.get("execution_blockers"))

    plan = snapshot.get("plan") if isinstance(snapshot.get("plan"), dict) else {}
    reference = snapshot.get("reference") if isinstance(snapshot.get("reference"), dict) else {}
    decision = {
        **base,
        "acceptance_complete": bool((snapshot.get("acceptance") or {}).get("complete")),
        "reference": {
            "policy": str(reference.get("policy") or "unknown"),
            "foundation_state": str(reference.get("foundation_state") or "not-applicable"),
            "attempts": int(reference.get("attempts") or 0),
            "max_attempts": int(reference.get("max_attempts") or 0),
            "productive_sessions": int(reference.get("productive_sessions") or 0),
            "stagnant_tail": int(reference.get("stagnant_tail") or 0),
        },
        "plan": {
            "complete": bool(plan.get("complete")),
            "blocked": bool(plan.get("blocked")),
            "planner_failures": int(plan.get("planner_failures") or 0),
        },
        "scheduler": _scheduler(snapshot.get("scheduler")),
        "eligible": eligible,
        "eligible_roles": eligible_roles,
        "split_required": split_required,
        "execution_blockers": blockers,
        "tests_complete": bool((snapshot.get("tests") or {}).get("complete")),
        "acceptance_validation_complete": bool(
            (snapshot.get("acceptance_validation") or {}).get("complete")
        ),
    }
    if snapshot.get("state_error"):
        decision["state_error_type"] = str(snapshot.get("state_error_type") or "")
        decision["state_error_message"] = str(snapshot.get("state_error_message") or "")[:500]

    compact = json.dumps(decision, sort_keys=True, separators=(",", ":"))
    if len(compact) > MAX_DECISION_CHARS:
        raise RuntimeError(
            f"materialized decision exceeds bound chars={len(compact)} max={MAX_DECISION_CHARS}"
        )

    views = {
        "decision.json": decision,
        "eligible.json": {
            **base,
            "scheduler": _scheduler(snapshot.get("scheduler")),
            "eligible": eligible,
            "eligible_roles": eligible_roles,
        },
        "blockers.json": {
            **base,
            "scheduler": _scheduler(snapshot.get("scheduler")),
            "execution_blockers": blockers,
        },
        "split.json": {**base, "split_required": split_required},
    }
    leaf_views = {
        did: _leaf_projection(did, leaf, roles.get(did, ""))
        for did, leaf in leaves.items()
        if isinstance(leaf, dict)
    }
    return views, leaf_views


def materialize_control_query_views(project, snapshot, manifest, source_rendered):
    project = Path(project)
    root = project / ".opencode-v2" / "query"
    leaf_root = root / "leaves"
    root.mkdir(parents=True, exist_ok=True)
    leaf_root.mkdir(parents=True, exist_ok=True)

    views, leaf_views = build_query_views(snapshot, manifest, source_rendered)
    leaf_contexts = build_leaf_contexts(project, manifest)

    # Publish leaf state + contract packets before decision.json. Therefore any
    # newly eligible leaf visible in the latest decision already has its packet.
    wanted = set()
    for did, payload in leaf_views.items():
        name = f"{did}.json"
        wanted.add(name)
        atomic_write_text(
            leaf_root / name,
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        )
    for did, payload in leaf_contexts.items():
        name = f"{did}-context.json"
        wanted.add(name)
        atomic_write_text(
            leaf_root / name,
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        )
    for path in leaf_root.glob("D*.json"):
        if path.name not in wanted:
            path.unlink(missing_ok=True)

    # decision.json is the publication barrier for the current scheduler view.
    for name, payload in views.items():
        if name == "decision.json":
            continue
        atomic_write_text(
            root / name,
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        )
    atomic_write_text(
        root / "decision.json",
        json.dumps(views["decision.json"], sort_keys=True, separators=(",", ":")) + "\n",
    )

    context_chars = [
        len(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        for payload in leaf_contexts.values()
    ]
    return {
        "decision_chars": len((root / "decision.json").read_text()),
        "leaf_count": len(leaf_views),
        "leaf_context_count": len(leaf_contexts),
        "max_leaf_context_chars": max(context_chars, default=0),
    }


def _selftest():
    snapshot = {
        "owner": "supervisor",
        "state_error": False,
        "resume_phase": "execution",
        "acceptance": {"complete": True},
        "reference": {
            "policy": "external-required",
            "foundation_state": "ready",
            "attempts": 1,
            "max_attempts": 3,
            "productive_sessions": 1,
            "stagnant_tail": 0,
        },
        "plan": {"complete": True, "blocked": False, "planner_failures": 1},
        "scheduler": {
            "max_concurrent_workers": 3,
            "active_workers": 1,
            "reserved_workers": 0,
            "available_worker_slots": 2,
            "active_deliverables": ["D002"],
            "error": "",
        },
        "leaves": {
            "D001": {"complete": True, "eligible": False, "attempt_ledger_valid": True},
            "D002": {"complete": False, "eligible": False, "running": True, "attempt_ledger_valid": True},
            "D003": {"complete": False, "eligible": True, "attempt_ledger_valid": True},
            "D004": {
                "complete": False,
                "eligible": False,
                "attempt_ledger_valid": True,
                "launch_deps_missing": ["D003"],
            },
        },
        "execution_blockers": [],
        "tests": {"complete": False},
        "acceptance_validation": {"complete": False},
    }
    manifest = {
        "leaves": {
            "D001": {"role": "probe-builder"},
            "D002": {"role": "core-builder"},
            "D003": {"role": "feature-builder"},
            "D004": {"role": "test-builder"},
        }
    }
    source = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
    views, leaves = build_query_views(snapshot, manifest, source)
    assert views["decision.json"]["eligible"] == ["D003"]
    assert views["decision.json"]["eligible_roles"] == {"D003": "feature-builder"}
    assert views["decision.json"]["reference"] == {
        "policy": "external-required",
        "foundation_state": "ready",
        "attempts": 1,
        "max_attempts": 3,
        "productive_sessions": 1,
        "stagnant_tail": 0,
    }
    assert views["decision.json"]["scheduler"]["available_worker_slots"] == 2
    assert leaves["D004"]["eligible"] is False
    assert "launch_deps_missing" in leaves["D004"]["ineligibility_reasons"]
    assert leaves["D003"]["role"] == "feature-builder"
    assert len(json.dumps(views["decision.json"], separators=(",", ":"))) < MAX_DECISION_CHARS

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        (project / ".opencode-v2" / "work").mkdir(parents=True)
        (project / ".opencode-v2" / "ACCEPTANCE.md").write_text(
            "# Acceptance Contract\n"
            "Reference policy: internal\n"
            "- [ ] A001: first required behavior\n"
            "- [ ] A002: second required behavior\n"
            "<!-- ACCEPTANCE_COMPLETE -->\n"
        )
        (project / ".opencode-v2" / "work" / "D004-A.scope.md").write_text(
            "# D004-A split-child scope\n\nChild scope: bounded child work\n"
        )
        context_manifest = {
            "leaves": {
                "D003": {
                    "name": "Feature leaf",
                    "outcome": "Create the feature.",
                    "role": "feature-builder",
                    "owned_artifacts": "`app/feature.js`",
                    "owned_artifact_paths": ["app/feature.js"],
                    "launch_deps": [],
                    "contract_deps": [],
                    "verify_deps": [],
                    "acceptance_ids": ["A001"],
                    "complexity": "S",
                    "repeated_operations": 1,
                    "deep_reasoning": False,
                    "verify_command": "node tests/feature.js",
                    "done_when": "Feature verification passes.",
                },
                "D004-A": {
                    "name": "Split child",
                    "outcome": "Complete bounded child work.",
                    "role": "core-builder",
                    "owned_artifacts": "`app/core.js`",
                    "owned_artifact_paths": ["app/core.js"],
                    "launch_deps": ["D003"],
                    "contract_deps": [],
                    "verify_deps": [],
                    "acceptance_ids": ["A002"],
                    "complexity": "S",
                    "repeated_operations": 1,
                    "deep_reasoning": False,
                    "verify_command": "node tests/core.js",
                    "done_when": "Core verification passes.",
                },
                "D004": {
                    "name": "Split parent",
                    "split_children": ["D004-A", "D004-B"],
                },
            }
        }
        contexts = build_leaf_contexts(project, context_manifest)
        assert set(contexts) == {"D003", "D004-A"}
        assert contexts["D003"]["acceptance_musts"] == [
            {"id": "A001", "text": "first required behavior"}
        ]
        assert contexts["D004-A"]["source_kind"] == "split-child"
        assert "bounded child work" in contexts["D004-A"]["split_scope"]
        assert contexts["D004-A"]["context_error"] == ""
        assert contexts["D003"]["contract_version"]
    print("materialized control-query selftest: OK")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--selftest"]:
        _selftest()
    else:
        raise SystemExit("usage: control_query_views.py --selftest")
