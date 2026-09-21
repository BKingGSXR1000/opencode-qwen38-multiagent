# Stage-A Experiment Log

Concise durable evidence only; do not turn this into a raw log dump.

## Baseline
Original rollback baseline: `6ea36392440e56583f2b3ea07fdf7c183d6d1dda` (`Integrate OpenCode v1.18.31 Stage-A transport baseline`) on `a2-v11831-integration`.

Later authorized Work/Codex development advanced through 21 commits to `b22c0977ae68fa44b2ed307cb2873bbbfc29af53` (`Fail closed on terminal semantic children`). The human explicitly authorized commit/push during that phase.

## D001 — golden execution-idempotency proof
Project: `/home/bking/AI/a2-controller-live-20260920-162341`
- root: `ses_f40cc6778ffehn2ZL5e8fMSTeb`
- state: `c446c7468b0fac66`
- execution: `052c71a7f1ed703f381a2897673a6996c7bdc433638237a94720964d7e5518df`
- child: `ses_f40cc60c6ffeRKsoVNjM4G3bnq`
- HTTP 204; durable intent; native-child bind; replay suppressed; exactly one child; Verify PASS; D001 ready.

## D002 — two genuine failures and real recursive split
Retained project: `/home/bking/AI/a2-controller-live-20260920-161347`

Attempt 1:
- child `ses_f40a7462bfferfSVnTnTSmqZsn`
- execution `a30b038158bc03b4f945edc3d3dbf2f184939bdee2b6f05aa853f0dd82c20836`
- genuine: `probe_research_loop_no_owned_progress tool_turns=5 limit=5`

Attempt 2:
- root `ses_f40754d70ffezNrSkxe8m3xvrE`
- child `ses_f40754a4affeQEp2TG2QxuuvvT`
- execution `bb2c72cac613cc7cab80eab14bfa9a2a7ac8a2e0d4045bcaaff70999df27d9ae`
- real local Qwen probe-builder
- genuine: `verify-failed-1`

Two genuine failures produced `split_required generation=1`, reason `genuine-failure-threshold`.

Real splitter:
- session `ses_f405d57f2ffedG7CuNoKbC4Nop`
- claim `prt_0bfa2a79e001iGLES0v7dk66Ro`
- transaction `7fea9d248b14d6ec99e5a625`
- children `D002-A`, `D002-B`

Both children completed; D002 parent finalized/rejoined; split state accepted. **Preserve D002; do not reset/replay either attempt.**

## Live consolidated preflight
Retained project: `/home/bking/AI/a2-controller-live-20260920-161347`
- root `ses_f3c3beb53ffePbOzYSMXSyiF48`
- `transport-root`, `v2noop/root-noop`
- state `dc08263ed2c1dd0b`
- static PASS; runtime PASS
- semantic POSTs 0; worker launches 0; child count 5->5; D002/controller ledgers unchanged
- dry route: `D004 blocked (attempt_limit_reached)`

## Absolute-filePath canary — first attempt
Disposable project: `/tmp/a2-absolute-permission-canary-20260921/project`
Runtime evidence: `/tmp/a2-absolute-permission-canary-20260921/runtime-live/`
- root `ses_f3c29567cffedEV3ZFgPW7vlUB`
- child `ses_f3c295455ffeWVnc9y0F1eeVgt`
- role `acceptance-planner`
- model `syv/qwen38-light-nothink`

Correction to early inspection: authoritative v1.18.31 `message`/`part`/`session` tables prove real Qwen ran, produced five assistant turns, and attempted four file calls. All were denied by OpenCode permission evaluation before filesystem execution.

Established root cause:
- `instance.directory = /tmp/a2-absolute-permission-canary-20260921/project`
- `instance.worktree = /`
- evaluator pattern was `tmp/a2-absolute-permission-canary-20260921/project/.opencode-v2/...`
- `.opencode-v2/**` did not match
- `/tmp/.../.opencode-v2/**` did not match because evaluator pattern has no leading slash
- catch-all deny correctly won

Upstream v1.18.31 behavior confirms a global non-VCS project uses `worktree = /`.

Conclusion: transient permissions must be projected into OpenCode's exact worktree-relative namespace; do not "fix" rule ordering and do not broaden `/tmp`.

## Absolute-filePath canary — worktree-relative projection PASS
Disposable project: `/tmp/a2-absolute-permission-canary-20260921-v2/project`
Runtime evidence: `/tmp/a2-absolute-permission-canary-20260921-v2/runtime/result.json`
- root: `ses_f3bfa9afaffeObuCJgfO0YMdK5` (`transport-root`, `v2noop/root-noop`)
- child: `ses_f3bfa9939ffeglWLXRLOfPS1gU` (`acceptance-planner`, `syv/qwen38-light-nothink`)
- static and live consolidated preflight PASS; preflight semantic POSTs 0 and workers 0
- predicted and live non-Git worktree: `/`
- evaluator allowed `tmp/a2-absolute-permission-canary-20260921-v2/project/.opencode-v2/**` for the owned absolute read
- evaluator allowed the exact projected acceptance path for the owned absolute write
- unowned same-project write, sibling-project read, outside-project read, and `..` escape were denied
- no unowned project file was created; deterministic acceptance finalization produced `ACCEPTANCE.ready`

The fixture deliberately has no `TEST_CHECKS.json`; the full project `run-checks.py --project` complaint is expected and does not weaken this permission or acceptance-finalization evidence.

## D004 provenance and deterministic recovery — 2026-09-21
Retained project: `/home/bking/AI/a2-controller-live-20260920-161347`

- D004's immutable ledger remains at three dispatches: one infrastructure
  dispatch and two later reclassified `bad-plan` entries from the runtime
  parent-contract repair. No D004 attempt was reset or replayed.
- The archived D004 split proved its child handoff found no VECTORS files.
  The current D004 Verify therefore cannot be satisfied by its manifest-only
  ownership; it must wait for producer recovery.
- The audit found D002 and D003 had readiness from earlier, weaker Verify
  commands, while the current plan requires real VECTORS artifacts. The
  retained filesystem has no vector files.
- `93c1e13` adds bounded, auditable replacement credits for changed Verify
  contracts and rejects no-op runtime plan repairs. The supervisor reconciled
  exactly `D002` and `D003` readiness markers; both ledgers retain their full
  dispatch/failure histories and gained one plan-contract revision credit.
- Resulting route: D003 is eligible; D002 remains an accepted split parent;
  D004 is correctly blocked on both producers. No semantic child was launched
  by the audit or reconciliation.

## D003 current-contract recovery — 2026-09-21
Retained project: `/home/bking/AI/a2-controller-live-20260920-161347`

- The retained native child `ses_f3bcd87c3ffe6FeRyO7dY4TB7Q` was created by
  root `ses_f3bcdf631ffe0pkyK0YvvvAkEP` for execution
  `e04c1e8f6de07063a8192c10ff7cfc3ae439c047e10ea77783a9dda660c53003`.
  It completed its OpenCode loop; no second child was created.
- Recovery exposed two generic reconciliation defects: controller replay did
  not bind an observed native child to its preclaim, and the exact
  role-expanded runtime prompt exceeded the canonical transport-prompt cap.
  Both are fixed with deterministic regression tests.
- The bounded plan-contract replacement credit is now applied before attempt
  validation. It preserves attempt 3 while recording its true
  `plan_contract_replacement` authority rather than consuming an operator
  retry.
- Exact current D003 Verify failed (`verify-failed-1`) because the required
  moon VECTORS files remain absent. With the historical genuine attempt 1,
  this produced durable `split_required generation=1`.
- Current dry routing selects only `task-splitter` for D003 generation 1.
  D002 and D004 were neither reset nor replayed.

## D003 splitter terminal recovery — 2026-09-21
Retained project: `/home/bking/AI/a2-controller-live-20260920-161347`

- The generation-1 splitter execution
  `f232cc0957b4428cd8eec9659ebe7d15c9b00789c641dba2882d930d8d0c9dd6`
  created child `ses_f3ba64c5cffeClIm3nbduaJJH3` under technical root
  `ses_f3ba6f5b0ffe6Z50bCKlc1dKoU`.
- The child used `task-splitter` with
  `syv/qwen38-implementation-planner-48k`, read `D003.split-request.json`,
  and reached OpenCode `finish=length` before returning the required bare JSON
  proposal. No proposal or split children were persisted.
- The background reconciliation loop was not running after that terminal
  event. The new `--reconcile-splits-once` recovery entrypoint transitioned
  only D003 from `splitter-active` to `split-retryable`, recording
  `splitter-completed-without-json-proposal`.
- D003's attempt ledger stayed at count 3 with the same three sessions and two
  genuine failures; the native semantic-child count stayed at 7. No model,
  server, or semantic dispatch was started by the one-shot recovery.

## D003 bounded splitter retry — 2026-09-21
Retained project: `/home/bking/AI/a2-controller-live-20260920-161347`

- Fresh technical root `ses_f3b8c01e7ffeT2Fqjj4eIDCnNX` passed the full
  consolidated preflight. The sole dry action was D003 generation-1
  `task-splitter`; preflight made zero semantic POSTs and created zero workers.
- Execution `5b21d79ba1fdb79e865065937de406db186516a68061d2474e8bff129751f49c`
  created child `ses_f3b8ac091ffe41tKijchkfRPmv`. As with the first splitter
  child, it read `D003.split-request.json` and ended `finish=length` at exactly
  1,536 output tokens without a proposal.
- Controller reconciliation initially exposed an implementation-only native
  binding call for task splitters. The scoped fix recognizes the durable native
  splitter child as replay evidence directly; it does not create an attempt or
  issue another POST.
- One-shot recovery then set D003 `splitter-failed`: claim count 2, proposal
  failures 2, reason `splitter-completed-without-json-proposal`. D003 remains
  at three worker dispatches and the retained child count remained 8.
