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
