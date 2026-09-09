---
description: V2 autonomous acceptance-planned, DAG-executed, acceptance-gated build.
agent: orchestrator
---

Build this project in the CURRENT directory:

$ARGUMENTS

Run the canonical V2 pipeline from your orchestrator instructions.

Hard rules:
- Pass the ORIGINAL USER REQUEST to acceptance-planner without adding new
  external-reference requirements.
- Do not proceed past Phase 0 without actual ACCEPTANCE.ready.
- Do not proceed past Phase 0.5 without actual IMPLEMENTATION_PLAN.ready.
- Dispatch exact planned Dxxx IDs using short disk-referenced worker prompts.
- Only exact ACCEPTANCE_PASS is success.
