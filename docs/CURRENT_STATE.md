# Current Stage-A State

Generated/updated by `install-opencode-project-memory.py`.

## Repository
- Repository: `/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831`
- Branch at generation time: `a2-v11831-integration`
- Current integration HEAD: `debb584` (`Reserve splitter final response step`)

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
The three original audited D003 generation-1 task-splitter children
`ses_f3ba64c5cffeClIm3nbduaJJH3` and
`ses_f3b8ac091ffe41tKijchkfRPmv`, followed by the single audited replacement
`ses_f3b7ca8abffeKAabDkiXerJ4Xp`, read the same split request and reached
OpenCode `finish=length` before emitting the required bare JSON proposal. They
consumed no D003 worker attempt and created no split children. One-shot durable
reconciliation recorded three splitter proposal failures and reached the finite
state `splitter-failed` (claim 3, including the single replacement budget). The
second execution was reconciled to its native child without replay after
correcting a controller bug that incorrectly sent task splitters to
implementation binding.

The output-cap prompt repair did not change this behavior. The authorized
splitter-only replacement profile keeps the same Qwen ID, 49,152-token context,
1,536-token output cap, and 7,168-token v2 cap, while disabling thinking.
Its single audited retained D003 recovery child
`ses_f3b4daf67ffeYS06u7O7cRWgF1` (execution
`2cf26fa53f303301a3bdc19ae90561c4709eb609dabd47040bcd5fc0f6baa95b`)
did not reach the output cap: it read the request, attempted an unsolicited
second `AGENTS.md` read, and the exact-one-tool guard denied it. With the old
three-step budget OpenCode then emitted a maximum-steps summary instead of JSON.
This is durable evidence of a guard/step-budget interaction, not a backend or
reasoning-cap failure. D003 is terminal again at splitter claim/failure count 4;
its worker attempt ledger remains count 3.

`debb584` reserves a fourth, final response turn while retaining exactly one
allowed tool call. A full-preflight synthetic disposable canary proved it:
`ses_f3b366ff0ffeb2FoF8GJ1AKlmE` read only its canonical split request, emitted
bare `v2-task-split-proposal-v2` JSON with zero reasoning tokens, and was
deterministically committed as D001-A/D001-B. This proves the repaired generic
splitter path but does not alter or reopen retained D003.

The separately authorized retained fifth claim then used that exact four-step
contract. Its child `ses_f3b19851cffelLxQ3TbYDBdlqQ` returned bare JSON with
zero reasoning tokens, but deterministic proposal validation rejected the
second child because `fixtures/vectors/moons/` was outside the generated child
ownership set. D003 is terminal at splitter claim/failure count 5 with
`split-validation-failed`; its worker attempt count remains 3. No further
splitter claim is authorized.

### Next action
Preserve D002 and every valid historical attempt/split record. D003 has no
remaining authorized splitter claim. A future change to its split contract or
ownership decomposition requires a new architectural decision; D004 remains
blocked through ordinary dependency resolution.

### Disposable splitter execution proof — 2026-09-21
`3770308` normalizes canonical directory ownership roots only for containment;
it does not broaden ownership. The follow-up splitter repair projects the
already-materialized canonical split request into the initial child context and
removes the splitter read capability. This prevents a denied duplicate read
from consuming the finite native step budget. Multiline prompt parsing is
consistent at the controller plugin and supervisor claim boundary.

The evidence-complete disposable canary
`/tmp/a2-splitter-directory-canary-20260921-v6/project` passed full preflight
with zero preflight POSTs/workers, then dispatched technical root
`ses_f3a72d505ffeWU3iyp1DKPmyL2` and splitter child
`ses_f3a7200ddffejayuckvdQo3tUZ`. The child used the intentional
`syv/qwen38-task-splitter-nothink` profile, made zero tool calls, emitted bare
proposal JSON, and was accepted as D001-A/D001-B. D001-B owns the exact
canonical directory root `fixtures/vectors/moons/`; exact-root containment is
also covered by the focused deterministic regression. The fixture supplied
authoritative failed-Verify evidence (exact command, exit 1, stderr, attempt
and session), preventing false parent-contract-invalid routing.

Retained D002, D003, and D004 were untouched. D003 remains at splitter
claim/failure 5 and worker attempt count 3. Claim 6 requires explicit approval.

### Retained D003 claim 6 — terminal safety finding (2026-09-21)

The explicitly authorized claim 6 ran only after a full consolidated preflight
on fresh technical root `ses_f3a5d877cffeRqd3Gmyc1977VG` (state version
`6ddcd8654de875b8`, zero preflight POSTs/workers). Execution
`1f8b3e915299e41946768097999f2873930162f1dfa2fa844e2daa45d559dada`
created native child `ses_f3a5c978dffe7XyaNxT4jBs8fB` using the intentional
`syv/qwen38-task-splitter-nothink` profile. The child made zero tool calls and
emitted bare JSON, but incorrectly classified the valid failed parent as
`v2-split-parent-contract-invalid-v1` / `prerequisite_artifacts`.

The old deterministic handler accepted that unproven model assertion,
archived the active split request/status into
`contract-repair-history/D003.1790022221982441752.json`, and rearmed D003 as a
runtime parent-contract repair even though the structured D003 contract did
not materially change. The archived record preserves the full claim-6 status
(`claim_count=6`, `proposal_failures=5`), prompt/request, and final JSON; D003
worker attempt count remains 3 and D002/D004 were not touched. No claim 7 was
issued and the runtime was stopped.

`3dbf4f0` closes that trust boundary: only a `verify_command` independently
rejected by the deterministic contract validator may request a parent-plan
repair. Missing parent-owned artifacts are split-recoverable work, not a
model-authorized repair path. Two focused regressions plus the 24-test
splitter/component suite and all static launcher selftests pass. A fresh
disposable live proof is still required before any future retained D003 claim
can be considered. The first post-fix copy-based disposable attempt was
correctly blocked in GET-only preflight because its legacy fixture marked the
plan ready while failing the current guard-manifest compatibility check; it
launched no semantic child and its runtime was stopped. Rebuild the canary
from a current guard-valid fixture rather than treating that stale fixture as
model evidence.

### Fresh current-state D003-equivalent splitter proof — 2026-09-21

`6ae5a80` adds `create-d003-splitter-canary.py`, which builds a fresh fixture
through current bootstrap, structured-plan compilation, finalized acceptance/
plan guards, query materialization, and supervisor-generated failed-Verify
threshold state. It does not copy ready markers, manifests, leases, queries,
or runtime metadata. The live fixture was
`/tmp/a2-d003-current-state-canary-20260921/project`.

Both consolidated preflights passed with zero preflight POSTs/workers. The
first splitter child `ses_f3a498003ffe01CO4v7Ct1aP91` (root
`ses_f3a4a1136ffeWhc7t9SXHKFTcN`) made zero tool calls and emitted bare JSON,
but again claimed a valid Verify was invalid. Deterministic validation rejected
it as `parent-contract-invalid verify_command lacks a deterministic contract
defect`; no repair packet or child contract was created, and the parent moved
normally to `split-retryable`.

After a fresh second preflight, child `ses_f3a47bdb4ffe4AS8FZ03UpHKKu` (root
`ses_f3a48c8d0ffe1s45Djs7dY2Jbo`) also used zero tools and emitted bare normal
proposal JSON. Strict validation accepted D003-A as a progress-only
`probe-builder` and D003-B as an `implementer` owning exactly
`.opencode-v2/probes/ephemeris_moons.json` and `fixtures/vectors/moons/`.
Its operation target `fixtures/vectors/moons/` passed exact directory-root
containment without broadened access. The parent reached split state `accepted`
at claim count 2 / proposal failure count 1. The disposable runtime was
stopped without launching implementation children.

Retained D002/D003/D004 were not accessed for mutation. A final read-only
check confirms retained D003 worker count remains 3; its claim-6 archive
preserves claim count 6. The next retained action would be claim 7 and needs
explicit authorization.

## Retained-state protection
Retained project: `/home/bking/AI/a2-controller-live-20260920-161347`

D002 is complete historical evidence; do not reset/replay it.

Current downstream blocker: D004 awaits valid current-contract completion of
D002 and D003. Its three historical dispatches remain intact; stale readiness
does not authorize a retry by itself. D003 requires a fresh disposable proof
of the new parent-contract-invalid guard and explicit authorization before any
future retained splitter claim.

## Configuration freeze
Until explicitly requested, keep worker/planner models (except restoring documented values), context sizes, concurrency, MTP, reasoning settings, and production vLLM tuning unchanged.

Task-splitter alone uses `syv/qwen38-task-splitter-nothink`; its context,
output, and v2 token caps match the prior splitter profile. All other model and
runtime settings remain frozen.

## Working method
Proceed autonomously through `ROADMAP.md`. Use local Qwen/OpenCode when it is the correct integration proof.

Keep Work context small: exact DB rows, exact session/event grep, bounded log/file ranges, targeted diffs; avoid ingesting whole run trees or giant logs.

Update this file after meaningful state changes so a fresh Work session can resume without reconstructing history.
