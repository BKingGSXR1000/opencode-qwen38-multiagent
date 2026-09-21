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

### Next action
Perform the read-only D004 provenance/blocker audit in `ROADMAP.md`, then apply only the deterministic recovery policy supported by durable state and architecture. Preserve D002 and every valid historical attempt/split record.

## Retained-state protection
Retained project: `/home/bking/AI/a2-controller-live-20260920-161347`

D002 is complete historical evidence; do not reset/replay it.

Current known blocker:
- D004
- attempts 3
- allowed attempts 3
- `attempt_limit_reached = true`

After permission stabilization, audit D004 provenance and apply intended deterministic recovery policy; do not mutate history merely to clear the blocker.

## Configuration freeze
Until explicitly requested, keep worker/planner models (except restoring documented values), context sizes, concurrency, MTP, reasoning settings, and production vLLM tuning unchanged.

Task-splitter remains `syv/qwen38-implementation-planner-48k`.

## Working method
Proceed autonomously through `ROADMAP.md`. Use local Qwen/OpenCode when it is the correct integration proof.

Keep Work context small: exact DB rows, exact session/event grep, bounded log/file ranges, targeted diffs; avoid ingesting whole run trees or giant logs.

Update this file after meaningful state changes so a fresh Work session can resume without reconstructing history.
