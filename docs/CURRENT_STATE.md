# Current Stage-A State

Generated/updated by `install-opencode-project-memory.py`.

## Repository
- Repository: `/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831`
- Branch at generation time: `a2-v11831-integration`
- HEAD at generation time: `b22c0977ae68fa44b2ed307cb2873bbbfc29af53`
- HEAD subject: `Fail closed on terminal semantic children`

Working tree at generation time:
```text
 M scripts/drive-stage-a-run.py
 M scripts/run-a2-v11831-server.sh
 M scripts/run-stage-a-tick.py
 M scripts/start-stage-a-run.sh
 M xdg/config/opencode/agents/task-splitter.md
?? scripts/stage_a_path_permissions.py
?? scripts/stage_a_preflight.py
?? scripts/test_stage_a_stabilization.py
```
This snapshot is informational; inspect live Git state before modifying or committing.

## Proven state
- deterministic technical root + native semantic children
- execution idempotency/reconciliation
- generic Dxxx implementation dispatch
- genuine attempt accounting
- real D002 recursive split + parent rejoin
- stabilization static/component suite
- live consolidated GET-only preflight

## Last known stabilization working set
The following were intentionally uncommitted at the last known checkpoint; verify live Git state rather than assuming this list is still exact:
- `scripts/drive-stage-a-run.py`
- `scripts/run-a2-v11831-server.sh`
- `scripts/run-stage-a-tick.py`
- `scripts/start-stage-a-run.sh`
- `xdg/config/opencode/agents/task-splitter.md`
- `scripts/stage_a_path_permissions.py`
- `scripts/stage_a_preflight.py`
- `scripts/test_stage_a_stabilization.py`

## Current resume point
Worktree-relative permission projection is proven in the current uncommitted stabilization working set.

Disposable live canary:
- project: `/tmp/a2-absolute-permission-canary-20260921-v2/project`
- root: `ses_f3bfa9afaffeObuCJgfO0YMdK5` (`transport-root`, `v2noop/root-noop`)
- child: `ses_f3bfa9939ffeglWLXRLOfPS1gU` (`acceptance-planner`, `syv/qwen38-light-nothink`)
- non-Git live worktree: `/`
- exact worktree-relative projection allowed the owned absolute read and write
- unowned same-project write, sibling-project read, outside-project read, and `..` escape were denied
- deterministic acceptance finalization produced `ACCEPTANCE.ready`
- static/component selftests and the live consolidated preflight passed; its preflight made zero semantic POSTs and launched zero workers

The acceptance-only fixture intentionally has no `TEST_CHECKS.json`; the full project `run-checks.py --project` complaint is not a canary blocker.

### Current recovery state
The D004 provenance audit established that its two semantic failures were later
reclassified as `bad-plan` during a parent-contract repair, but old code still
counted those immutable dispatches against the automatic retry budget. The
repair packet was also discarded after syntax compilation without proving an
affected leaf changed.

`93c1e13` fixes both generic control-plane defects and binds readiness to the
exact Verify command. On 2026-09-21 the retained supervisor reconciled only
the stale `D002` and `D003` readiness markers, recording one bounded
plan-contract replacement credit for each. No session, attempt, failure, or
split record was reset or removed.

- D003 is now the next eligible semantic producer.
- D002 remains an accepted historical split parent whose current parent Verify
  must be re-established before downstream use.
- D004 remains blocked on D002 and D003; it is not eligible to retry or to
  fabricate producer artifacts.

### Latest retained recovery — D003
The stale current-contract D003 readiness was intentionally revoked under the
bounded Verify-revision policy. Its existing attempt 3 was then reconciled
without creating another child:
- root `ses_f3bcdf631ffe0pkyK0YvvvAkEP`
- child `ses_f3bcd87c3ffe6FeRyO7dY4TB7Q` (`probe-builder`,
  `syv/qwen38-worker-nothink`)
- execution `e04c1e8f6de07063a8192c10ff7cfc3ae439c047e10ea77783a9dda660c53003`

The native child completed, wrote its owned probe artifact, and failed the
exact current Verify because required vector artifacts remain absent. This is
the second genuine D003 failure; `split_required generation=1` is durable.
Attempt 3 is a supervisor plan-contract replacement, not an operator retry.
The current deterministic action is `task-splitter` for D003 generation 1.

### Next action
Refresh the live supervisor projection, run consolidated preflight, then
advance only the deterministic D003 task-splitter action. Preserve D002 and
every valid historical attempt/split record.

## Retained-state protection
Retained project: `/home/bking/AI/a2-controller-live-20260920-161347`

D002 is complete historical evidence; do not reset/replay it.

Current downstream blocker: D004 awaits valid current-contract completion of
D002 and D003. Its three historical dispatches remain intact; stale readiness
does not authorize a retry by itself.

## Configuration freeze
Until explicitly requested, keep worker/planner models (except restoring documented values), context sizes, concurrency, MTP, reasoning settings, and production vLLM tuning unchanged.

Task-splitter remains `syv/qwen38-implementation-planner-48k`.

## Working method
Proceed autonomously through `ROADMAP.md`. Use local Qwen/OpenCode when it is the correct integration proof.

Keep Work context small: exact DB rows, exact session/event grep, bounded log/file ranges, targeted diffs; avoid ingesting whole run trees or giant logs.

Update this file after meaningful state changes so a fresh Work session can resume without reconstructing history.
