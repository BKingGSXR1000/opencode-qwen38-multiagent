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
- [x] restore task-splitter model to `syv/qwen38-implementation-planner-48k`
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
- [ ] audit D004's 3 attempts / `attempt_limit_reached` provenance without rewriting history
- [ ] determine correct existing deterministic next state: authorized retry, recursive split, repair, or terminal block
- [ ] fix generic controller behavior if it cannot express the intended policy
- [ ] progress remaining deliverables using normal Stage-A scheduling
- [ ] preserve exact Verify and attempt evidence
- [ ] never use D002 as a disposable test target

## F. Continuous/generic operation
- [ ] prove generic driver advances across multiple action types without manual orchestration
- [ ] prove restart/resume from durable state
- [ ] prove no duplicate semantic dispatch after ambiguous transport outcomes
- [ ] prove preflight cannot be bypassed by normal launcher/driver entrypoints
- [ ] validate concise terminal/block reporting
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
