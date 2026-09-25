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

## D003 audited output-limit replacement — 2026-09-21

- The output-cap role instruction was loaded by a fresh server/config overlay.
  A new full consolidated preflight passed with zero semantic POSTs and zero
  workers, then authorized the single output-limit replacement claim.
- Execution `3bebb84cebb4d642cbc90e7f5969e1859908ae021ac1e622171124d6abd369dc`
  created `ses_f3b7ca8abffeKAabDkiXerJ4Xp`. It again read only the split request
  and reached `finish=length` at 1,536 output tokens with no final text or
  proposal. The prompt repair was therefore not causal relief.
- The active restored `qwen38-implementation-planner-48k` configuration has
  output limit 1,536 and `chat_template_kwargs.enable_thinking=true`. The
  persisted child parts are reasoning-only despite the role's JSON-only rule.
- Final one-shot reconciliation set D003 `splitter-failed` at claim count 3
  and proposal failure count 3. D003's worker attempt ledger stayed at count 3
  and its retained semantic-child count stayed at 9. No further replacement
  claim was made.

## D003 profile recovery and synthetic splitter canary — 2026-09-21

Retained project: `/home/bking/AI/a2-controller-live-20260920-161347`

- `8ea4431` introduced a task-splitter-only non-thinking profile with the same
  Qwen model ID, context/output limits, and v2 token cap as the failed planner
  profile. Its one-time recovery is bound to durable prior/current profile
  fingerprints; no generic retry was opened.
- Full retained-project preflight passed on fresh technical root
  `ses_f3b517761ffePA9Q71JpEyB0pH` with zero preflight POSTs/workers. The sole
  resulting dispatch `2cf26fa53f303301a3bdc19ae90561c4709eb609dabd47040bcd5fc0f6baa95b`
  created `ses_f3b4daf67ffeYS06u7O7cRWgF1`.
- That child used `syv/qwen38-task-splitter-nothink`, had zero reasoning tokens,
  and completed the one permitted request read. It then attempted an unsolicited
  read of the ephemeral overlay `AGENTS.md`; the exact-one-tool guard denied it.
  Its final output was OpenCode's maximum-step summary rather than proposal JSON.
  D003 therefore reached the finite state `splitter-failed` at claim/failure 4;
  its worker attempt ledger stayed at count 3 and no split child was created.

Disposable project: `/tmp/a2-splitter-step-canary-20260921/project`

- `debb584` reserves a fourth final-response step while retaining the exact
  one-tool guard. Full preflight passed on technical root
  `ses_f3b375c0bffeBIeKxxLy6e2enG` with zero preflight POSTs/workers.
- The sole task-splitter child `ses_f3b366ff0ffeb2FoF8GJ1AKlmE` performed only
  the canonical request read, emitted bare proposal JSON (`finish=stop`, zero
  reasoning tokens), and deterministically committed D001-A/D001-B with exact
  non-overlapping ownership (`a.txt` / `b.txt`). Its canary runtime was stopped.
- This validates the generic splitter lifecycle and does not reset, replay, or
  reopen D002, D003, D004, or any retained attempt ledger.

## D003 authorized four-step execution-contract claim — 2026-09-21

Retained project: `/home/bking/AI/a2-controller-live-20260920-161347`

- `7e149b6` added a separately bounded recovery that accepts only the exact
  adjacent 3→4 task-splitter execution-contract transition. Before dispatch it
  preserved D003 at worker attempt count 3 and splitter claim/failure count 4,
  recorded prior/current execution-contract fingerprints, and raised only the
  finite splitter recovery budget for claim 5.
- Full consolidated preflight passed on technical root
  `ses_f3b19ff60ffe8A8m8LJ8q8VfKW` at state version
  `6ddcd8654de875b8`, selecting only D003 generation-1 task-splitter with zero
  preflight POSTs/workers. Execution
  `2b8b48455102ac29c1cd5bea7aa1541844a8808017809df20551ad35b965dd57`
  created `ses_f3b19851cffelLxQ3TbYDBdlqQ`.
- The child used `syv/qwen38-task-splitter-nothink`, read only the canonical
  split request, had zero reasoning tokens, and emitted bare proposal JSON.
  It also made one denied duplicate request-read; the four-step contract still
  reserved enough room for the final JSON response.
- Deterministic validation rejected the proposal exactly because its second
  child declared `fixtures/vectors/moons/` in `creates_or_updates` while that
  path was outside the generated child ownership set. The proposal is preserved
  as `D003.split-proposal.failed-5.json`; no child contract was committed.
- D003 is now finite `split-validation-failed` at claim/failure count 5 and
  worker attempt count 3. The runtime was stopped. No further splitter claim is
  authorized; D002 and D004 were not modified.

## Disposable direct-context splitter proof — 2026-09-21

- `3770308` fixed strict containment for a canonical owned directory root
  (`fixtures/vectors/moons/`) when an operation target omits only its trailing
  slash. It did not expand ownership.
- The splitter request is now supplied as initial canonical context and the
  splitter has no read permission. This removes the duplicate-read/tool-step
  loop without salvaging non-bare final output. Controller, plugin, and
  supervisor all accept the same multiline `SPLIT_PARENT` form.
- Evidence-complete disposable project:
  `/tmp/a2-splitter-directory-canary-20260921-v6/project`. Its request included
  a supervisor-generated exact failed Verify record (exit 1, exact command,
  stderr, attempt 2, session `synthetic-worker-2`) plus the genuine-failure
  threshold and canonical ownership inventory.
- Full preflight passed with zero worker launches. Root
  `ses_f3a72d505ffeWU3iyp1DKPmyL2` launched child
  `ses_f3a7200ddffejayuckvdQo3tUZ` on
  `syv/qwen38-task-splitter-nothink`. The child made zero tool calls, emitted
  bare JSON, and deterministically committed D001-A/D001-B. D001-B owns exact
  `fixtures/vectors/moons/` and creates the bounded required record within it.
- The nonthinking profile is intentional (`8ea4431`): same underlying Qwen,
  context/output/v2 caps as the former planner profile, differing only by
  disabled thinking. Retained D002/D003/D004 were not accessed or changed.

## Retained D003 claim 6 — parent-contract-invalid safety finding — 2026-09-21

- The authorized claim used fresh root `ses_f3a5d877cffeRqd3Gmyc1977VG` after
  full preflight PASS (state `6ddcd8654de875b8`, zero preflight POSTs/workers).
  Execution `1f8b3e915299e41946768097999f2873930162f1dfa2fa844e2daa45d559dada`
  created child `ses_f3a5c978dffe7XyaNxT4jBs8fB` on the intentional
  `syv/qwen38-task-splitter-nothink` profile.
- The child made zero tool calls, completed with `finish=stop`, and emitted a
  bare `v2-split-parent-contract-invalid-v1` object. Its claim that missing
  JSON files inside parent-owned `fixtures/vectors/moons/` implied no legal
  child owner was false: that directory is explicit D003 ownership and a
  valid split target under `3770308`.
- The previous handler trusted that assertion and archived the active status,
  request, and final payload under
  `contract-repair-history/D003.1790022221982441752.json`; the archive records
  claim 6 exactly. No child contracts were created, no claim 7 was issued, and
  D003 worker attempts stayed at 3. D002 and D004 were untouched.
- `3dbf4f0` rejects model-authored prerequisite-artifact repair requests and
  accepts a parent-plan repair only when deterministic validation finds an
  invalid `verify_command`. The focused regression suite (24 tests) and the
  preflight/controller/tick static selftests passed. The runtime was stopped;
  a disposable live proof remains pending before any retained follow-up.
- A copied older disposable fixture was then used only for GET-only preflight.
  Its legacy `IMPLEMENTATION_PLAN.ready`/guard combination fails the current
  guard-manifest compatibility check, so no semantic child was launched and
  its runtime was stopped. It is not evidence about the splitter model or the
  new guard; rebuild the fixture from current guard-valid inputs.

## Fresh current-state D003-equivalent splitter proof — 2026-09-21

- `6ae5a80` introduced a reusable fixture builder that starts from current
  bootstrap/control-state machinery, current structured-plan compilation, and
  current finalized guards. It seeds only canonical predecessor readiness and
  supervisor-recorded D003 failed-Verify evidence; no legacy runtime metadata
  is copied.
- Disposable project: `/tmp/a2-d003-current-state-canary-20260921/project`.
  Its initial D003 state was split-required, generation 1, two genuine failed
  exact Verify records, and canonical ownership
  `.opencode-v2/probes/ephemeris_moons.json` plus
  `fixtures/vectors/moons/`.
- Both fresh-root consolidated preflights passed with zero semantic
  POSTs/workers. First child `ses_f3a498003ffe01CO4v7Ct1aP91` made zero tool
  calls and emitted bare parent-contract-invalid JSON. The new deterministic
  validator rejected the false `verify_command` assertion; no repair packet,
  reclassification, or split child was created, and the finite state became
  split-retryable.
- Second child `ses_f3a47bdb4ffe4AS8FZ03UpHKKu`, on the same intentional
  nonthinking splitter profile, also made zero tool calls and emitted bare
  normal proposal JSON. Strict validation committed D003-A (progress-only
  probe) and D003-B (writer owning exactly the probe JSON and
  `fixtures/vectors/moons/`). Exact directory-root containment passed and the
  parent reached `accepted` at splitter claim count 2 / failure count 1.
- No disposable implementation child was launched; the proof is intentionally
  limited to the splitter proposal/contract transaction. The runtime stopped.
  Retained D002/D003/D004 were not mutated. Read-only retained evidence still
  reports D003 worker count 3 and archived claim-6 count 6.

## Retained D003 claim 7 and bounded child split — 2026-09-22

- `cf42c3f` reconstructed only D003's archived false-parent-repair split edge,
  preserving the historical worker ledger and claims 1-6 while allowing exactly
  claim 7. `7e07d1a` preserved the false live repair packet in a resolution
  archive and finalized the unchanged plan without launching a planner.
- Full preflight passed with root `ses_f385d96d6ffer4kvY0zZ8oRWnW`, state
  `2587bd5b93943346`, one D003 splitter action, and zero preflight workers.
  Execution `685ccd22dc2b6381670237f9db39f6a53e7086503205675aef4ed33a1d4b0a58`
  created `ses_f3858a822ffeGy1JA5T7e6d0MS` on the approved no-thinking profile.
  Strict validation accepted D003-A and D003-B; D003 is `accepted` at claim 7 /
  failure 5, with no claim 8.
- D003-A finalized. D003-B attempts 1 and 2 both genuinely failed exact Verify;
  attempt 2's supervisor stderr proves its probe was not JSON. Its generation-1
  splitter then consumed its ordinary two claims: the first had no bare JSON;
  the second (`ses_f3846296effeGhJtc3cXlN5aAx`) emitted a false invalid-contract
  object with an overlong reason. Strict validation rejected it; no repair,
  grandchildren, D003 completion, or D004 eligibility change occurred.
- `b288300` added a precise role constraint against treating failed artifact
  contents or formatting speculation as a parent Verify defect. Focused static
  tests pass, but disposable project
  `/tmp/a2-d003b-splitter-prompt-canary/project` still produced two false
  invalid-contract assertions under the real local Qwen. The runtime was
  stopped. This establishes a prompt-only limitation, not a permission,
  transport, ownership, or scheduler failure.
- During the initial claim-7 preflight supervisor startup, two pre-existing
  historical sessions outside this D003 path (D005/D007) were observed and
  classified as infrastructure-aborted for noncanonical runtime handoff. No
  new semantic session was launched for either. This is an isolation concern
  for future retained preflight runs; D002/D003/D004 accounting was unaffected.

## Disposable bounded corrective-native-child transport — 2026-09-22

- The generalized correction implementation preserves a single logical
  splitter claim while allowing at most one separately bound corrective native
  `task-splitter` child. Its intent records the primary session, root, ordinal
  `1`, state/reason identity, and canonical request context; no deterministic
  split synthesis or extra recovery claim is permitted.
- Focused splitter suite: PASS (26 tests). Python and plugin syntax checks:
  PASS. Fresh disposable D003-B-equivalent preflight passed with zero semantic
  POSTs/workers. The primary model response was deterministically invalid, and
  corrective intent persisted with claim count 1 / corrective count 1.
- The corrective root `prompt_async` returned HTTP 204, but no second native
  child appeared. The POST is therefore ambiguous and was not replayed. The
  live end-to-end proof remains incomplete; do not use this as authorization
  for retained D003-B recovery. Retained D003-B/D003/D002/D004 were untouched
  and the disposable runtime was stopped.

## Full deterministic completion + restart/crash recovery — 2026-09-25

- Fresh R6 project `/home/bking/AI/a2-e2e/20260925-081317-deterministic-full-r6/project`
  reached durable `complete`: final tests PASS, acceptance validation complete,
  zero execution blockers, and a hash-bound `acceptance-pass.json` covering all
  20 MUST checks.
- Supervisor-only live restart proof:
  `/home/bking/AI/a2-restart-canary/20260925-113631-supervisor-restart-fixed`.
  D001 was busy at crash with attempt count 1 / one child / no failures. After
  supervisor restart, the same child remained busy and unique, with no retry or
  failure charged. Scheduler recovered one active worker and two free slots.
  That child later completed and READY was minted on attempt 1. Restarting the
  supervisor again after completion did not rerun D001.
- The first restart attempt exposed a bug in orphan classification: a pending
  pre-restart child was treated as server-lost even while still active.
  `a65156b` fixes the predicate and adds a regression for that exact case.
- True OpenCode-server + supervisor crash proof:
  `/home/bking/AI/a2-restart-canary/20260925-114140-server-crash-real`.
  The actual listener PID on port 58479 and supervisor were SIGKILLed while D001
  was busy; the port was confirmed closed. After both restarted, the old child
  was absent from `/session/status` and was classified once as infrastructure
  (`opencode-server-restart-incomplete-session`), never as a genuine failure.
  D001 became eligible with 3 free scheduler slots. One replacement attempt was
  dispatched (attempt 2), produced the exact sentinel artifact, and finalized
  READY. Root child set is exactly {lost original, replacement}; no third child
  exists. `server-crash-final-proof.json` records `pass=true`.
- Final regression sweep: control-plane 126 PASS, state-machine 79 PASS, Stage-A
  stabilization 41 PASS, historical New51 replay 6 PASS over 121 archived
  decisions, transport-root 5 PASS, plus run-checks/finalizer/controller/
  restart-canary selftests PASS.

## Corrective native-child transport live closure — 2026-09-25

- Ran `scripts/run-d003-corrective-canary.sh` against the current local backend.
- Disposable project: `/home/bking/AI/a2-canaries/20260925-124620-d003b-corrective/project`.
- The deterministic malformed primary response consumed one logical splitter claim and one corrective turn only.
- The technical root materialized exactly two native children total: the primary and one corrective child.
- Final split status reached `accepted` with `corrective_dispatch_state=native-child-completed`; no third child or synthetic continuation was observed.
- Canary emitted `D003_CORRECTIVE_CANARY_PASS` and exited 0. This closes the prior HTTP-204-without-child ambiguity without authorizing any retained D003-B replay.
