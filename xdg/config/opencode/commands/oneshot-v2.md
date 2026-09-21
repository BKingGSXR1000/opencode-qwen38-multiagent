---
description: Retired legacy command. Start generic Stage-A runs with scripts/start-stage-a-run.sh.
agent: transport-root
---

Build this project in the CURRENT directory:

=== ORIGINAL_USER_TASK_BEGIN ===
$ARGUMENTS
=== ORIGINAL_USER_TASK_END ===

Run the canonical V2 pipeline from your orchestrator instructions.

Hard rules:
- Pass the ORIGINAL USER REQUEST to acceptance-planner without adding new
  external-reference requirements.
- Do not proceed past Phase 0 without actual ACCEPTANCE.ready.
- Do not proceed past Phase 0.5 without actual IMPLEMENTATION_PLAN.ready.
- Dispatch exact planned Dxxx IDs using short disk-referenced worker prompts.
- Only exact ACCEPTANCE_PASS is success.

<!-- 20260911 ORIGINAL TASK DURABILITY FIX BEGIN -->
## Durable original-task rule — authoritative

The text between:

`=== ORIGINAL_USER_TASK_BEGIN ===`

and:

`=== ORIGINAL_USER_TASK_END ===`

is the ORIGINAL USER TASK.

It remains the authoritative task for the entire V2 run, including after
root-session recycling, supervisor continuation, compaction, planner repair,
or any other control-plane transition.

Never replace that task with a message such as "Continue orchestration for
this project."

When `.opencode-v2/ORIGINAL_TASK.md` exists, that file is the immutable
authoritative copy of the original user task.
<!-- 20260911 ORIGINAL TASK DURABILITY FIX END -->

<!-- V2.6.9 EXACT DELIVERABLE HANDOFF BEGIN -->
## Exact deliverable handoff

Every implementation child prompt must use the concrete canonical ID in ALL
five lines. Never send the literal placeholder `Dxxx` to a child.

For deliverable D042 the exact shape is:

DELIVERABLE: D042
Read your D042 section in .opencode-v2/IMPLEMENTATION_PLAN.md.
Read .opencode-v2/work/D042.progress.md if present.
Inspect your owned project artifacts as they currently exist.
Continue from actual filesystem state and execute the deliverable.

Substitute only the actual canonical ID. The supervisor rejects placeholder or
model-derived variants before the child starts.
<!-- V2.6.9 EXACT DELIVERABLE HANDOFF END -->
