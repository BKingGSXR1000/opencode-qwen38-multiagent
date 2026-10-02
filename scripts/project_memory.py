#!/usr/bin/env python3
"""Opt-in, source-grounded selective memory for Stage-A leaf context packets.

Never turns a worker summary into authority. Records immutable plan/Acceptance
revisions and supervisor-validated READY checkpoints with a tamper-evident,
append-only local event history. Retrieval is scoped to declared dependencies.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from control_state import ready_info
from state_io import StateCorruptionError, atomic_write_json, exclusive_file_lock

PROTOCOL = "v1-selective-project-memory"
MARKER = "v1-selective-project-memory"
EVENT_PROTOCOL = "v1-selective-project-memory-event"
PACKET_PROTOCOL = "v1-selective-project-memory-retrieval"
MAX_EVENTS = 10000
MAX_HISTORY_BYTES = 32 * 1024 * 1024
MAX_PACKET_CHARS = 4200
MAX_VERIFIED_DEPENDENCIES = 8
FIELDS = (
    "name", "outcome", "role", "owned_artifact_paths", "launch_deps",
    "contract_deps", "verify_deps", "acceptance_ids", "verify_command",
    "done_when", "parent", "split_children", "split_handoff_only",
    "split_handoff_source",
)


def _canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha(data):
    return hashlib.sha256(
        data.encode("utf-8") if isinstance(data, str) else data
    ).hexdigest()


def _source_root(project):
    return Path(project).resolve() / ".opencode-v2" / "work" / "selective-project-memory"


def enabled(project):
    path = Path(project) / ".opencode-v2" / "work" / "selective-memory.enabled"
    return path.is_file() and not path.is_symlink() and path.read_text().strip() == MARKER


def _safe_file(path):
    if path.is_symlink():
        raise StateCorruptionError(f"selective memory symlink rejected: {path}")


def _events(path):
    _safe_file(path)
    if not path.exists():
        return [], ""
    if path.stat().st_size > MAX_HISTORY_BYTES:
        raise StateCorruptionError("selective memory history exceeds safe read bound")
    events = []
    previous = ""
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if len(events) >= MAX_EVENTS:
                raise StateCorruptionError("selective memory history event limit")
            try:
                event = json.loads(line)
            except (ValueError, TypeError) as exc:
                raise StateCorruptionError("selective memory history malformed") from exc
            if not isinstance(event, dict):
                raise StateCorruptionError("selective memory history row not object")
            expected = event.get("sha256")
            body = {k: v for k, v in event.items() if k != "sha256"}
            if (
                body.get("protocol") != EVENT_PROTOCOL
                or body.get("sequence") != len(events) + 1
                or body.get("previous_sha256") != previous
                or not isinstance(body.get("payload"), dict)
                or not isinstance(expected, str)
                or expected != _sha(_canonical(body))
            ):
                raise StateCorruptionError("selective memory history hash chain invalid")
            events.append(event)
            previous = expected
    return events, previous


def _index(path, project_digest, events):
    _safe_file(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise StateCorruptionError("selective memory index malformed") from exc
    if not isinstance(data, dict):
        raise StateCorruptionError("selective memory index not object")
    digest = data.get("index_sha256")
    bare = {k: v for k, v in data.items() if k != "index_sha256"}
    if (
        data.get("protocol") != PROTOCOL
        or data.get("owner") != "supervisor"
        or data.get("project_sha256") != project_digest
        or digest != _sha(_canonical(bare))
        or data.get("events_tip") not in (
            {""} | {event["sha256"] for event in events}
        )
    ):
        raise StateCorruptionError("selective memory index integrity invalid")
    return data


def _append(path, previous, sequence, kind, key, payload):
    _safe_file(path)
    body = {
        "protocol": EVENT_PROTOCOL, "sequence": sequence, "previous_sha256": previous,
        "kind": kind, "key": key, "payload": payload,
    }
    event = {**body, "sha256": _sha(_canonical(body))}
    encoded = (_canonical(event) + "\n").encode("utf-8")
    if sequence > MAX_EVENTS:
        raise StateCorruptionError("selective memory history event limit reached before append")
    current_bytes = path.stat().st_size if path.exists() else 0
    if current_bytes + len(encoded) > MAX_HISTORY_BYTES:
        raise StateCorruptionError("selective memory history byte limit reached before append")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(encoded.decode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    return event


def _leaf_contract(leaf):
    return {
        field: leaf.get(field) for field in FIELDS
        if field in leaf
    }


def _checkpoint(project, did, leaf, snapshot_leaf):
    if not (isinstance(snapshot_leaf, dict) and snapshot_leaf.get("complete") is True):
        return None
    # Projection's 'complete' is a hint, not an authority. Re-evaluate READY,
    # including current artifact provenance and transitive dependency readiness.
    current = ready_info(project, did)
    if not current:
        return None
    command = str(leaf.get("verify_command") or "")
    marker_sha = str(current.get("verify_sha256") or "")
    if not command or (marker_sha and marker_sha != _sha(command)):
        return None
    return {
        "attempt": int(current["attempt"]),
        "verify_sha256": _sha(command),
        "artifact_sha256": str(current.get("artifact_sha256") or ""),
        "ready_sha256": _sha(
            (Path(project) / ".opencode-v2" / "work" / f"{did}.ready").read_bytes()
        ),
        "source": "supervisor-validated-current-READY",
    }


def _audited_attempts(project, leaves):
    """Durable supervisor classifications and contract-repair checkpoints only."""
    path = Path(project) / ".opencode-v2" / "work" / "attempts.json"
    if not path.exists():
        return {}, {}, ""
    _safe_file(path)
    raw = path.read_bytes()
    try:
        ledger = json.loads(raw)
    except ValueError as exc:
        raise StateCorruptionError("selective memory attempt ledger malformed") from exc
    if not (
        isinstance(ledger, dict) and ledger.get("owner") == "supervisor"
        and isinstance(ledger.get("deliverables"), dict)
    ):
        raise StateCorruptionError("selective memory attempt ledger untrusted")
    failures, recoveries = {}, {}
    for did, entry in sorted(ledger["deliverables"].items()):
        if did not in leaves or not isinstance(entry, dict):
            continue
        raw_failures = entry.get("failure_history") or []
        raw_repairs = entry.get("plan_contract_revisions") or []
        if not isinstance(raw_failures, list) or not isinstance(raw_repairs, list):
            raise StateCorruptionError("selective memory attempt history invalid")
        rows = []
        for failure in raw_failures:
            if not isinstance(failure, dict) or failure.get("source") != "supervisor":
                continue
            try:
                attempt = int(failure.get("attempt") or 0)
            except (TypeError, ValueError):
                continue
            if attempt < 1:
                continue
            rows.append({
                "attempt": attempt,
                "session": str(failure.get("session") or "")[:100],
                "classification": str(failure.get("classification") or "")[:36],
                "reason": str(failure.get("reason") or "")[:360],
                "timestamp": str(failure.get("timestamp") or "")[:40],
                "recovered_by": str(failure.get("recovered_by") or "")[:80],
                "reclassified_by": str(failure.get("reclassified_by") or "")[:80],
                "source": "supervisor-attempt-ledger",
            })
        if rows:
            failures[did] = rows
        receipts = []
        for repair in raw_repairs:
            if not isinstance(repair, dict) or not str(repair.get("source") or "").startswith("supervisor-"):
                continue
            try:
                attempt = int(repair.get("attempt") or 0)
            except (TypeError, ValueError):
                continue
            if attempt < 1:
                continue
            receipts.append({
                "attempt": attempt,
                "source": str(repair["source"])[:100],
                "producer": str(repair.get("producer") or "")[:36],
                "previous_result": str(repair.get("previous_result") or "")[:65],
                "timestamp": str(repair.get("timestamp") or "")[:40],
            })
        if receipts:
            recoveries[did] = receipts
    return failures, recoveries, _sha(raw)


def refresh(project, manifest, acceptance, snapshot=None):
    """Refresh active index, retaining all earlier revisions in the hash-chain log.

    Invoked once per control-query materialization, only for explicitly
    enabled projects. Atomic lock covers event append and index replacement.
    """
    project = Path(project).resolve()
    if not enabled(project):
        return None
    leaves = manifest.get("leaves") if isinstance(manifest, dict) else None
    if not isinstance(leaves, dict):
        raise StateCorruptionError("selective memory manifest is invalid")
    root = _source_root(project)
    for path in (root, root / "lock"):
        if path.is_symlink():
            raise StateCorruptionError("selective memory root symlink rejected")
    digest = _sha(str(project))
    ctrl = project / ".opencode-v2"
    acceptance_path = ctrl / "ACCEPTANCE.md"
    manifest_path = ctrl / "IMPLEMENTATION_PLAN.guard.json"
    acceptance_sha = _sha(acceptance_path.read_bytes())
    manifest_sha = _sha(manifest_path.read_bytes())
    failures, recoveries, attempts_sha = _audited_attempts(project, leaves)
    # Only create persistent state after every current source has passed its
    # structural/ownership check; untrusted ledger input must have no writes.
    root.mkdir(parents=True, exist_ok=True)
    requirements = acceptance.get("musts", {}) if isinstance(acceptance, dict) else {}
    if not isinstance(requirements, dict):
        raise StateCorruptionError("selective memory Acceptance projection invalid")
    if isinstance(snapshot, dict):
        snapshot_leaves = snapshot.get("leaves", {})
    else:
        # Direct packet readers need the same current-READY semantics as the
        # supervisor materializer, not an empty projection that revokes history.
        snapshot_leaves = {
            did: {"complete": bool(ready_info(project, did))}
            for did in leaves if isinstance(did, str)
        }
    verified = {}
    for did, leaf in sorted(leaves.items()):
        if not isinstance(leaf, dict) or not isinstance(did, str):
            continue
        if (state := _checkpoint(project, did, leaf, snapshot_leaves.get(did))) is not None:
            verified[did] = state

    with exclusive_file_lock(root / "lock"):
        events, tip = _events(root / "events.jsonl")
        previous_tip = tip
        old = _index(root / "index.json", digest, events)
        # Recover an append that completed just before an index-write crash.
        latest = {}
        for event in events:
            latest[(event["kind"], event["key"])] = _sha(
                _canonical(event["payload"])
            )
        desired = [
            ("requirement", key, {"text": text, "acceptance_sha256": acceptance_sha})
            for key, text in sorted(requirements.items())
            if isinstance(key, str) and isinstance(text, str)
        ] + [
            ("decision", did, {
                "contract": _leaf_contract(leaf),
                "source_manifest_sha256": manifest_sha,
            })
            for did, leaf in sorted(leaves.items())
            if isinstance(did, str) and isinstance(leaf, dict)
        ]
        for did, rows in sorted(failures.items()):
            for row in rows:
                desired.append((
                    "attempt", f"{did}#{row['attempt']}#{row['session']}", row
                ))
        for did, rows in sorted(recoveries.items()):
            for row in rows:
                desired.append((
                    "recovery", f"{did}#{row['attempt']}#{row['source']}#{row['producer']}",
                    row,
                ))
        # Contract revisions are per-leaf, not every unrelated manifest rebuild.
        for kind, key, payload in desired:
            if kind == "decision":
                payload = {"contract": payload["contract"]}
            identity = (kind, key)
            version = _sha(_canonical(payload))
            if latest.get(identity) == version:
                continue
            row = _append(root / "events.jsonl", tip, len(events) + 1, kind, key, payload)
            events.append(row)
            tip = row["sha256"]
            latest[identity] = version

        old_verified = old.get("verified", {}) if isinstance(old.get("verified"), dict) else {}
        for did in sorted(set(old_verified) | set(verified)):
            was, now = old_verified.get(did), verified.get(did)
            if was == now:
                continue
            kind = "verified-checkpoint" if now else "checkpoint-revoked"
            payload = now if now else {"previous_ready_sha256": was.get("ready_sha256", "")}
            row = _append(root / "events.jsonl", tip, len(events) + 1, kind, did, payload)
            events.append(row)
            tip = row["sha256"]
        index = {
            "protocol": PROTOCOL, "owner": "supervisor",
            "project_sha256": digest,
            "acceptance_sha256": acceptance_sha,
            "manifest_sha256": manifest_sha,
            "requirements": requirements,
            "decisions": {
                did: _leaf_contract(leaf)
                for did, leaf in sorted(leaves.items())
                if isinstance(did, str) and isinstance(leaf, dict)
            },
            "verified": verified,
            "attempts_sha256": attempts_sha,
            "failures": failures,
            "recoveries": recoveries,
            "events_count": len(events),
            "events_tip": tip,
        }
        index["index_sha256"] = _sha(_canonical(index))
        if index != old:
            atomic_write_json(root / "index.json", index)
        return index


def _dependencies(did, decisions):
    """Prioritize exact Verify/handoff prerequisites before broad launch history.

    Some real plans declare every earlier leaf as a launch dependency. Plain
    breadth-first traversal would fill the bounded worker packet with D001..D005
    and omit the most relevant recent D010. Preserve the full declared closure
    in the index while ranking only the bounded retrieved subset.
    """
    def direct(leaf):
        ranked = [
            *(leaf.get("verify_deps") or []),
            *([leaf["split_handoff_source"]] if leaf.get("split_handoff_source") else []),
            *(leaf.get("contract_deps") or []),
            *([leaf["parent"]] if leaf.get("parent") else []),
            *sorted(leaf.get("launch_deps") or [], reverse=True),
        ]
        return [dep for dep in dict.fromkeys(ranked) if dep in decisions]

    result = []
    queue = [did]
    seen = {did}
    while queue:
        current = queue.pop(0)
        for dep in direct(decisions.get(current, {})):
            if dep not in seen:
                seen.add(dep)
                result.append(dep)
                queue.append(dep)
    return result


def _diagnostic_code(reason):
    """Extract only a mechanical token, never model-authored free-form prose."""
    token = str(reason or "").split(maxsplit=1)[0] if str(reason or "").strip() else ""
    return token[:80] if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,79}", token) else "unclassified"


def select(project, did, index):
    """Small, deterministic worker view. Never treats old results as current."""
    if not index or did not in index.get("decisions", {}):
        return {}
    project = Path(project).resolve()
    if index.get("project_sha256") != _sha(str(project)):
        raise StateCorruptionError("selective memory cross-project access denied")
    decisions = index["decisions"]
    dependencies = _dependencies(did, decisions)
    verified = index.get("verified", {})
    chosen = [dep for dep in dependencies if dep in verified][:MAX_VERIFIED_DEPENDENCIES]
    records = []
    for dep in chosen:
        decision = decisions[dep]
        checkpoint = verified[dep]
        records.append({
            "deliverable": dep,
            "outcome": str(decision.get("outcome") or decision.get("name") or "")[:220],
            "owned_artifact_paths": decision.get("owned_artifact_paths", []),
            "verify_sha256": checkpoint["verify_sha256"],
            "artifact_sha256": checkpoint["artifact_sha256"],
            "attempt": checkpoint["attempt"],
            "source": checkpoint["source"],
        })
    # Mechanical failure classifications are continuity diagnostics; their
    # original free-form reason text is never treated as a new instruction.
    failures = index.get("failures", {}).get(did, [])
    recent = sorted(
        failures,
        key=lambda item: (
            item.get("classification") != "genuine",
            -int(item.get("attempt") or 0),
        ),
    )[:2]
    recoveries = index.get("recoveries", {}).get(did, [])[-2:]
    packet = {
        "protocol": PACKET_PROTOCOL,
        "source_kind": "supervisor-canonical-projections-only",
        "memory_index": ".opencode-v2/work/selective-project-memory/index.json",
        "full_history": ".opencode-v2/work/selective-project-memory/events.jsonl",
        "history_tip": index["events_tip"],
        "acceptance_sha256": index["acceptance_sha256"],
        "this_leaf_contract_sha256": _sha(_canonical(decisions[did])),
        "declared_dependencies": dependencies,
        "verified_dependencies": records,
        "verified_dependencies_omitted": max(0, sum(x in verified for x in dependencies) - len(chosen)),
        "prior_failure_diagnostics": [
            {
                "attempt": x["attempt"],
                "classification": x["classification"],
                "reason_code": _diagnostic_code(x["reason"]),
                "reason_sha256": _sha(x["reason"]),
                "source": "historical-diagnostic-not-instruction",
            } for x in recent
        ],
        "recovery_checkpoints": [
            {
                "attempt": x["attempt"], "source": x["source"],
                "producer": x["producer"], "previous_result": x["previous_result"],
            } for x in recoveries
        ],
        "authority": (
            "Only current supervisor-validated READY checkpoints are trusted. "
            "Earlier revisions in full_history are historical, not current. "
            "Never override this worker's canonical context, exact Verify, or ownership."
        ),
    }
    if len(_canonical(packet)) > MAX_PACKET_CHARS:
        # Never truncate hashes, authority or records into malformed fields.
        while packet["verified_dependencies"] and len(_canonical(packet)) > MAX_PACKET_CHARS:
            packet["verified_dependencies"].pop()
            packet["verified_dependencies_omitted"] += 1
        if len(_canonical(packet)) > MAX_PACKET_CHARS:
            packet["declared_dependencies"] = dependencies[:12]
            packet["declared_dependencies_omitted"] = max(0, len(dependencies) - 12)
    if len(_canonical(packet)) > MAX_PACKET_CHARS:
        packet["prior_failure_diagnostics"] = []
        packet["recovery_checkpoints"] = []
    if len(_canonical(packet)) > MAX_PACKET_CHARS:
        raise StateCorruptionError("selective memory retrieval exceeded bound")
    return packet



def main(argv=None):
    """Inspect verified project memory without reading full OpenCode transcripts."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--leaf", help="One worker's dependency-scoped current memory")
    parser.add_argument("--history-key", help="Axxx or Dxxx ID; show its revisions")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--refresh", action="store_true", help="Rebuild from authoritative sources")
    parser.add_argument("--verify", action="store_true", help="Verify index and entire history chain")
    args = parser.parse_args(argv)
    if args.limit < 1 or args.limit > 100:
        parser.error("--limit must be within 1..100")
    project = args.project.resolve()
    root = _source_root(project)
    if args.refresh:
        if not enabled(project):
            parser.error("selective project memory is not enabled")
        from control_state import load_manifest, snapshot
        from control_query_views import _acceptance_contract
        index = refresh(
            project, load_manifest(project), _acceptance_contract(project),
            snapshot=snapshot(project),
        )
    else:
        events, _ = _events(root / "events.jsonl")
        index = _index(root / "index.json", _sha(str(project)), events)
        if not index:
            parser.error("no project memory index; enable and use --refresh")
    events, tip = _events(root / "events.jsonl")
    if tip != index["events_tip"] or len(events) != index["events_count"]:
        raise StateCorruptionError("selective memory index/history not in sync")
    ctrl = project / ".opencode-v2"
    if (
        index["manifest_sha256"] != _sha((ctrl / "IMPLEMENTATION_PLAN.guard.json").read_bytes())
        or index["acceptance_sha256"] != _sha((ctrl / "ACCEPTANCE.md").read_bytes())
    ):
        raise StateCorruptionError("selective memory source changed; use --refresh")
    output = {
        "protocol": PROTOCOL,
        "project_sha256": index["project_sha256"],
        "event_count": index["events_count"],
        "verified_checkpoint_count": len(index["verified"]),
        "history_tip": tip,
    }
    if args.leaf:
        # This is a read-only inspector. Avoid showing a stale READY receipt
        # if the current project changed since the last supervisor projection.
        scoped = _dependencies(args.leaf, index["decisions"]) if args.leaf in index["decisions"] else []
        validated = dict(index)
        current = {}
        for dep in scoped:
            checkpoint = index["verified"].get(dep)
            if not checkpoint:
                continue
            info = ready_info(project, dep)
            path = ctrl / "work" / f"{dep}.ready"
            if (
                info
                and int(info.get("attempt") or 0) == checkpoint["attempt"]
                and str(info.get("artifact_sha256") or "") == checkpoint["artifact_sha256"]
                and path.is_file()
                and _sha(path.read_bytes()) == checkpoint["ready_sha256"]
            ):
                current[dep] = checkpoint
        validated["verified"] = current
        output["leaf"] = select(project, args.leaf, validated)
        if not output["leaf"]:
            parser.error(f"unknown deliverable {args.leaf!r}")
    if args.history_key:
        key = args.history_key
        selected = [
            event for event in events
            if event["key"] == key or event["key"].startswith(key + "#")
        ]
        output["history"] = selected[-args.limit:]
        output["history_count_for_key"] = len(selected)
    print(json.dumps(output, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
