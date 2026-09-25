# Stage-A Roadmap

Status: `[x]` proven, `[~]` partial, `[ ]` remaining.

Continue autonomously through this roadmap. Fix/test as required; do not stop merely because a defect was found when the intended fix follows from `ARCHITECTURE.md`.

## A. Deterministic control plane
- [x] technical root `transport-root` + `v2noop/root-noop`
- [x] native semantic child creation/parenting
- [x] deterministic action selection
- [x] durable state-version-bound execution intent
- [x] ambiguous-dispatch replay suppression / reconciliation
- [x] generic implementation dispatch beyond D001
- [x] deterministic semantic/planner execution paths
- [x] trusted final-test routing
- [x] terminal semantic-child fail-closed guard
- [x] project-scoped supervisor lifecycle
- [x] generic bootstrap/root/tick/driver/launcher infrastructure

## B. Retry and recursive split
- [x] genuine worker-failure accounting
- [x] D002 reached genuine-failure threshold
- [x] durable split_required generation 1
- [x] native task-splitter execution
- [x] D002-A / D002-B generated with ownership
- [x] split children completed
- [x] D002 parent reconciled/finalized/rejoined
- [x] isolated stabilization split/rejoin test
- [x] preserve D002 as historical evidence; never reset/replay its attempts

## C. Stabilization
- [x] task-splitter-only non-thinking profile with unchanged Qwen/context/output/v2 caps
- [x] remove prompt-only relative-path workarounds
- [x] add path containment/ownership helper
- [x] hard consolidated-preflight gating
- [x] terminal-child tests
- [x] isolated split/rejoin test
- [x] live GET-only consolidated preflight on retained A2 project

## D. Current blocker — worktree-relative permission projection
- [x] prove failed canary reached real Qwen + file-tool permission evaluation
- [x] prove v1.18.31 uses last matching permission rule
- [x] prove file tools evaluate `path.relative(instance.worktree, resolved_file_path)`
- [x] prove non-Git disposable project used `worktree = /`
- [x] explain why old relative + leading-slash absolute aliases both failed
- [x] patch transient projection to exact worktree-relative evaluator namespace while canonical ownership remains PROJECT-relative
- [x] tests: Git PROJECT==worktree; non-Git worktree `/`; nested Git subproject; owned read/write; unowned same-project denial; sibling denial; `..` escape; symlink escape; no broad rule
- [x] live preflight verifies actual OpenCode worktree/project equals prediction
- [x] rerun one disposable absolute-filePath acceptance-planner canary
- [x] prove owned absolute read succeeds
- [x] prove owned absolute write succeeds
- [x] prove narrow negative cases remain denied
- [x] remove obsolete absolute-alias machinery if superseded

## E. Resume retained Stage-A progression
Only after D passes:
- [x] audit D004's 3 attempts / `attempt_limit_reached` provenance without rewriting history
- [x] determine correct existing deterministic next state: repair stale contracts and advance their producers before D004
- [x] fix generic controller behavior: runtime repair credits, no-op repair rejection, and exact Verify-bound readiness
- [~] progress remaining deliverables using normal Stage-A scheduling
  - [x] D003 splitter profile recovery is bounded, fingerprinted, and audited.
    The retained fourth claim exposed a one-tool guard/three-step interaction,
    then returned D003 to finite `splitter-failed` without changing its worker
    attempts. A full-preflight synthetic canary proves the repaired four-step
    generic splitter path creates and reconciles owned child contracts.
  - [x] one explicitly authorized D003 four-step execution-contract claim
    produced JSON but failed exact child-ownership validation. It is preserved
    as `split-validation-failed` at claim/failure 5; no worker attempt changed.
  - [x] prove direct-context/no-tool splitter execution and exact directory
    ownership containment in an evidence-complete disposable canary. Do not
    reset attempts, replay D002, or grant D003 claim 6 without authorization.
  - [~] D003 claim 6 proved the direct-context transport, but the model emitted
    an unproven parent-contract-invalid result. `3dbf4f0` makes parent-plan
    repair deterministically verify-command-only.
  - [x] fresh current-state D003-equivalent disposable canary: bootstrap/
    guard/query formats current; first false parent-contract-invalid is
    rejected without repair; bounded retry emits accepted direct-context,
    zero-tool normal proposal; D003-A/D003-B contracts materialize with exact
    moons directory ownership.
  - [x] authorized retained D003 claim 7 accepted the same ownership-safe
    split; D003-A completed canonically and D003-B ran through its normal
    deterministic child-worker retry/split policy without a D003 claim 8.
  - [~] D003-B child split exhausted its normal two splitter claims: a missing
    JSON proposal followed by a false oversized parent-contract-invalid result.
    The guard rejected both without repair or grandchildren. A fresh disposable
    canary proves the current local splitter can repeat false invalid-contract
    assertions despite prompt tightening. Choose a deterministic fallback or
    revised bounded-claim policy before any further retained recovery.
  - [x] bounded corrective-child transport is live-proven: one logical claim
    permits exactly a primary plus one separately intent-bound native corrective
    splitter child, without another claim or recovery budget. The deterministic
    D003-B canary on 2026-09-25 produced exactly two native root children,
    reached corrective_dispatch_state=native-child-completed, and observed zero
    synthetic continuation messages.
- [x] preserve exact Verify and attempt evidence
- [x] never use retained D002 as a disposable test target

## F. Continuous/generic operation
- [ ] prove generic driver advances across multiple action types without manual orchestration
- [x] prove restart/resume from durable state
- [x] prove no duplicate semantic dispatch after ambiguous transport outcomes
- [x] prove preflight cannot be bypassed by normal launcher/driver entrypoints
- [x] validate concise terminal/block reporting
- [ ] final static/component/integration suite
- [ ] update project memory to final proven state

## G. Deferred performance work — outside correctness stabilization
Do not mix into current work unless explicitly requested:
- Swift-Qwen3.8 comparison
- concurrency 3 -> 2
- larger per-agent context
- MTP changes
- alternate inference engines/quantizations
- broad performance tuning
