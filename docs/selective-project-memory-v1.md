# Selective project memory v1 — Stage-A harness

Status: opt-in. The deterministic scheduler, canonical plan, exact Verify and
supervisor READY provenance remain authoritative. This is structured continuity
separate from model conversation history, not a semantic LLM supervisor.

## Activation and storage

Enable per project with a marker at:

    .opencode-v2/work/selective-memory.enabled

Its entire content is the single line v1-selective-project-memory. It may
exist during bootstrap; activation waits until the guarded implementation
manifest has executable leaves. With no marker, existing worker packets are
unchanged. Disabling it stops projection without erasing existing history.

Source: scripts/project_memory.py. Local store:

    .opencode-v2/work/selective-project-memory/
        index.json
        events.jsonl
        lock

## Canonical sources and historical facts

The latest index binds project, Acceptance and manifest source SHA-256 hashes.
It retains normalized per-leaf contract decisions and only CURRENT
control_state.ready_info-validated checkpoint records, including verified
attempt, Verify SHA-256, current READY SHA-256 and owned-artifact provenance.
A revoked, modified or stale READY is never returned as a current result.

The append-only SHA-256 hash-chained event history preserves revisions of
Acceptance requirements, individual leaf contracts, READY changes, verified
supervisor failure classifications and recorded supervisor contract repairs.
The history begins on activation; prior OpenCode transcripts and tools remain
searchable in the original OpenCode DB, not imported into this layer.
The hash chain detects corruption and unsanctioned edits without chain
recalculation, but is NOT a digital signature against an attacker who can
rewrite both source records and the full local chain.

An exclusive process lock, fsync of event append, and atomic current-index
replacement make replay idempotent even if the index replacement crashes.
Malformed history, owner mismatch, or cross-project access fails closed.

Worker packets do not include raw old error prose. Only bounded mechanical
reason codes and hashes plus limited supervisor repair receipts are included.
Historical text stored on disk remains diagnostic, never a new instruction.

## Retrieval in existing worker packets

Each leaf's existing query/leaves/Dxxx-context.json gets one additional
selective_project_memory field after opt-in. It selects only current verified
members of the leaf's DECLARED prerequisite closure. Priority is exact Verify
dependencies, split handoff, contract dependencies, split parents, then
newest broad launch dependencies. Unrelated leaves and other projects are
excluded. Maximum: eight verified records and 4,200 characters; any omissions
are explicit. A path to the full memory index and history is retained.

No changes to scheduler decisions, roles, permissions, attempt limits,
finalization, exact Verify, or acceptance validity are authorized by memory.

## Local read-only inspection

From the A2 repository, set PYTHONPATH=scripts and run:

    python3 scripts/project_memory.py --project /absolute/project --verify
    python3 scripts/project_memory.py --project /absolute/project --leaf D010
    python3 scripts/project_memory.py --project /absolute/project --history-key D010 --limit 20
    python3 scripts/project_memory.py --project /absolute/project --refresh --leaf D010

The CLI validates the full history chain and current source hashes.
Leaf queries additionally revalidate current READY and artifact provenance.
Refresh requires this specific project's marker.

## Validation and known limits

Targeted tests cover opt-out compatibility; a marker installed before initial
planning; source provenance; declared-dependency retrieval; ranking; revocation;
bounded packets; multi-project isolation; historical revisions; append-before-
index crash replay; malformed supervisor ledgers; corrupted history and index;
sanitized old failures and recovery receipts; CLI readback; and materialized
worker packets. The full Stage-A regression suite is also required.

Retained Proof2 canary: 25 supervisor-verified checkpoints, bounded retrieval
for D011-B2 included the directly relevant D010 README producer, and all
13 acceptance MUSTs plus ACCEPTANCE_PASS remained valid. This proves
deterministic integration, not higher Qwen coding quality.

Future experiments: matched no-memory vs compaction vs selective retrieval on
long-horizon real repositories, latency/token overhead, confidence-aware
semantic summaries, and independent multi-GPU agent admission control.
