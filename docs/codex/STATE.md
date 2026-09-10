# OpenCode V2.6.9 — Current State

Branch: v2.6.9-development

## Stable architecture

- Filesystem is authoritative; conversations are disposable.
- Acceptance planner -> ACCEPTANCE.md -> deterministic guard.
- Implementation planner -> IMPLEMENTATION_PLAN.md -> deterministic guard.
- Orchestrator dispatches specialized workers.
- Workers own explicit artifacts plus their own Dxxx.progress.md.
- Completion is represented by .opencode-v2/work/Dxxx.ready.
- leaf-complete performs deterministic verification/finalization.
- Parent-visible subagent output is bounded; full child history remains stored.
- Dispatch is supervisor-preclaimed before worker execution.
- Root/workers cannot grant themselves extra attempts.
- Recursive split trees are supervisor-owned and persisted in the validated
  manifest plus `.opencode-v2/work/splits.json`; filesystem restart reconstructs
  the tree without session history.
- A dedicated `task-splitter` writes exactly two proposals, while the supervisor
  derives canonical child IDs, validates ownership/dependencies, and persists
  the split plus a child scope artifact. Split parents remain authoritative and
  run their original verify command only after required children are ready.

## Attempts / retries

- Current autonomous cap historically was 3 attempts.
- Explicit human retry grants exist through operator-control.py.
- /retry-failed is the intended user-facing operator retry command.
- Human grants must remain explicit and auditable.
- Infrastructure/runtime failures must be distinguished from genuine worker failures.
- Historical attempt counts must never be reset or rewritten.
- Depths 0 and 1 split after their second genuine failure; terminal depth 2
  permits three genuine automatic attempts and is then execution-blocked.
  Infrastructure/runtime and bad-plan outcomes are recorded but do not trigger
  splitting.

## Known current problems

1. Large leaves can repeatedly hit OpenCode maximum-step limits.
2. Retrying the identical oversized leaf is inefficient.
3. Probe-builder has shown excessive research before writing its owned artifact.
4. Infrastructure cancellation must not silently waste scarce operator authorization.
5. INTERNAL reference policy must not accidentally turn into external JPL/NASA research.

## Important existing controls to preserve

- strict role/canonical-prompt validation
- beta Dxxx placeholder normalization
- project scoping via location.directory
- parent-result bounding
- durable progress
- post-session mechanical finalization
- bounded infrastructure recovery
- execution-blocked status
- human/operator retry grants
- root/worker denial of operator-control authority

## Source control

Before changes, inspect git status and diffs.
Do not discard existing source changes.
Never use `git restore .`.

Do not commit runtime files such as:
- current-gpu-log.txt
- current-server-log.txt
- *.pid
- xdg/config/vllm/usage_stats.json

Do not modify or run:
/home/bking/AI/gametest2y
unless explicitly instructed.

`scripts/operator-control.py` and `scripts/operator_control.py` are
intentionally both retained: the hyphenated file is the stable human-facing CLI
entrypoint, while the underscored file is its importable implementation.
