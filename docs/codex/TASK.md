# Completed Protocol — Bounded Recursive Leaf Splitting

Status: implemented and covered by deterministic control-plane regression
tests. This document records the required behavior for future changes; it is
not a pending implementation request.

The system automatically performs bounded decomposition of implementation
leaves that repeatedly fail because their scope is too large.

## Required policy

Depth 0:
- genuine attempt 1 fails -> retry same leaf
- genuine attempt 2 fails -> split unfinished work into 2 children

Depth 1:
- genuine attempt 1 fails -> retry
- genuine attempt 2 fails -> split only that failing branch into 2 children

Depth 2:
- never split further
- allow 3 genuine autonomous attempts
- then execution-blocked

Maximum split depth: 2.

Example:

D001
├── D001-A
│   ├── D001-A1
│   └── D001-A2
└── D001-B
    ├── D001-B1
    └── D001-B2

## Failure classification

Infrastructure/runtime failure:
- use bounded infrastructure recovery
- does NOT count toward split threshold

Genuine worker failure:
- verification failure after meaningful work
- maximum-step exhaustion after meaningful work
- useful partial progress but unfinished
- DOES count toward split threshold

Bad/invalid plan:
- do not split
- enter bounded plan-repair handling

## Split semantics

- Split only unfinished work, not prompt text.
- Existing completed work must not be duplicated.
- Prefer parallel children when genuinely independent.
- Encode dependencies when required.
- Original parent remains authoritative.
- Original parent verification must remain unchanged.
- Parent becomes ready only when required children are ready AND original parent verification passes.
- Splitting may not weaken acceptance criteria.

## Implemented splitter handoff

A dedicated bounded task-splitter agent is supervisor-preclaimed once per
pending parent/generation. It reads one durable request and writes exactly one
structured proposal. Its completion hook immediately invokes supervisor
validation/persistence; a missing or invalid proposal becomes an explicit,
finite split failure rather than a waiting state.

Input should include:
- parent ID/depth
- parent scope
- ownership
- verification
- durable progress
- existing artifacts
- compact summaries of failed attempts

Output exactly two structured child proposals.

Supervisor must validate and persist the split.
The model may not invent arbitrary IDs or directly mutate supervisor control state.

Use deterministic hierarchical IDs such as:
- D001-A / D001-B
- D001-A1 / D001-A2

## Attempts and history

- Preserve all historical parent attempts.
- Children have independent attempt histories.
- Never reset counts when splitting.
- Operator retries continue to work on executable leaves at every depth.
- Human retry must never be self-granted by root/worker.

## Scheduling

Independent split children may run concurrently under the normal concurrency limit.
Only the failing branch should recurse.

## Worker resume

Strengthen resume instructions to say:
- continue from durable filesystem state
- do not repeat completed investigation
- prioritize remaining owned artifacts and verification
- child workers must stay inside their child scope

## Regression coverage

The deterministic control-plane suite covers:
- retry after first genuine failure
- split after second genuine failure at depth 0
- split after second genuine failure at depth 1
- no split beyond depth 2
- depth-2 three-attempt limit
- infrastructure failures not causing split
- bad plan not causing split
- ownership validation
- no cycles
- parent verification preserved
- parent not ready merely because children are ready
- parent finalizes when children ready + original verify passes
- independent children runnable concurrently
- restart reconstructs tree from filesystem
- operator retry still works
- no self-grant
- all existing tests remain green

The disposable real-Qwen/OpenCode smoke must prove when run:
parent -> 2 failures -> split -> children execute -> parent verifies -> ready.

Do NOT run Jupiter.
Do NOT modify gametest2y.

After successful tests:
- commit relevant source/config/test/docs changes only
- push v2.6.9-development

Resolve whether both operator-control.py and operator_control.py are intentionally required before committing.

End successful report with:
RECURSIVE_SPLIT_READY
