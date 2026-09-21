# Stage-A Architecture — Authoritative Invariants

## 1. Authority split

**Deterministic controller/supervisor** owns scheduling semantics: phase/action selection, eligibility/dependencies, attempt accounting, retry classification, recursive split transitions, completion/reconciliation, final-test routing, and replay safety.

**Semantic workers (local Qwen)** perform bounded semantic planning, research, implementation, validation, or splitting.

**`transport-root`** is technical transport only:
- agent: `transport-root`
- model: `v2noop/root-noop`
- controller explicitly pins both
- native OpenCode `task`/`SubtaskPart` creates and parents semantic children
- no semantic LLM root orchestrator
- no homemade parallel child-session transport

## 2. Canonical paths and ownership

Durable project ownership/contracts use PROJECT-relative paths, e.g. `.opencode-v2/ACCEPTANCE.md`, `.opencode-v2/probes/env_probe.json`, `src/foo.ts`.

Do not store absolute `/home/...`, `/tmp/...`, or incidental `./` spellings as canonical ownership identities. A role may mutate only its declared owned artifacts; alternate path spelling must never expand that set.

## 3. OpenCode permission namespace

OpenCode v1.18.31 file tools resolve supplied `filePath`, then evaluate permission against:

`path.relative(instance.worktree, resolved_file_path)`

Keep two namespaces distinct:
1. canonical ownership: PROJECT-relative;
2. transient OpenCode evaluator projection: relative to effective `instance.worktree`.

Examples:
- Git PROJECT == worktree: `.opencode-v2/ACCEPTANCE.md` stays `.opencode-v2/ACCEPTANCE.md`.
- Non-Git PROJECT `/tmp/test/project`, worktree `/`: evaluator pattern becomes `tmp/test/project/.opencode-v2/ACCEPTANCE.md`.
- PROJECT nested under Git worktree `/home/user/repo`: evaluator pattern becomes `subproject/.opencode-v2/ACCEPTANCE.md`.

Projection must be exact-project scoped. Never use broad `/tmp/**`, `tmp/**`, `/home/**`, or similar grants.

Live preflight must verify actual OpenCode project/worktree identity matches the assumed projection before semantic launch.

## 4. `/tmp` rule

`/tmp` is for ephemeral runtime/config/cache/log/socket/disposable-test data only. It grants zero durable project authority. A PROJECT under `/tmp` still uses canonical PROJECT-relative ownership and exact worktree-relative permission projection.

## 5. Filesystem authority

Worker sandbox/supervisor remains filesystem authority. Do not rely on LLM prompt obedience for ownership protection. Do not weaken project containment, role ownership, cross-project denial, failed-worker handling, or Verify.

## 6. Dispatch and replay safety

- Persist execution intent before native POST.
- Identity binds state version + technical root + canonical action.
- Use native `prompt_async + SubtaskPart`.
- Pin `transport-root` + `v2noop/root-noop` explicitly.
- Reconcile native children.
- Never blindly replay ambiguous POSTs.
- Terminal semantic children fail closed if the expected durable transition did not occur.

## 7. Attempts, retry, splitting

Attempt accounting is durable evidence. Do not reset/manufacture attempts. Keep infrastructure failures distinct from genuine semantic failures. Retry decisions are deterministic. At genuine-failure threshold, recursive split is deterministic/generation-bound, children have explicit ownership, and parent completion requires child reconciliation/finalization.

D002 is the canonical proven real example; preserve it.

## 8. Verify and final tests

Canonical Verify is contractual. Execute exact repository-owned verification; never weaken Verify to make a worker pass. Known fixture defects may be corrected only when independently established and only in the intended fixture. Final tests use trusted repository-owned `run-checks.py` plus durable evidence.

## 9. Consolidated preflight

No real semantic-worker launch until consolidated preflight passes. It covers source state, backend/auth, canonical runtime/config identity, exact permission projection, technical root/model, supervisor exact-project binding, canonical query materialization, selector/shadow agreement, state version, controller dry decision, owned-artifact state, Verify viability, and proof that preflight itself performs no semantic dispatch.

Fail closed if actual OpenCode project/worktree differs from predicted projection.

## 10. Configuration freeze

Unless explicitly requested, do not change model choices, context sizes, concurrency, MTP, reasoning mode/budget, or production vLLM tuning.

Task-splitter profile: `syv/qwen38-task-splitter-nothink`. It uses the same
underlying Qwen ID, context, output cap, and v2 cap as the historic planner
profile, but intentionally disables thinking for bounded JSON splitting.
