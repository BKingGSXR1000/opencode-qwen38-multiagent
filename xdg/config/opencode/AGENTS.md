# Qwen3.8 Multi-Agent V2.5.2

Execution:
- Parent: non-thinking dispatcher.
- Normal implementation/test/state children: non-thinking.
- Review/investigation/integration: low thinking.
- reasoning-builder: medium thinking, implementation-only.
- At most two heavy workers.
- One primary deliverable per child.
- Normal follow-up always gets a FRESH child.
- task_id reuse exactly once only after supervisor interrupt.
- First child compaction retires that child.
- `execute`/CodeMode disabled; direct tools only.

Acceptance architecture:
1. acceptance-planner — specification only, NO web/research/calculation.
2. implementation workers.
3. reference-researcher — independent authoritative external truth; web allowed;
   cannot modify product code.
4. acceptance-validator — mechanically tests every MUST against app + reference.
5. finalize-acceptance.py — creates acceptance-pass.json only on real PASS.

Rules:
- Planner defines WHAT must be proven, not the astronomy/science answer.
- Researcher obtains authoritative external truth needed for validation.
- Validator compares the product independently against that truth.
- Failed validation creates narrow repair tasks, then a fresh validator.
- No ACCEPTANCE_PASS = no DONE.

<!-- V2.5.6 CORE CAPABILITY BOUNDARY BEGIN -->
## Core capability / external-service boundary

Do not silently replace a core requested capability with an external runtime
service/API when doing so materially changes what is being implemented.

- External sources used for research, development, or independent validation
  are NOT automatically permitted as production/runtime dependencies.
- If the user's request or acceptance contract explicitly requires or permits
  an external runtime service, using it is allowed.
- If the user's request explicitly requires local/offline/self-contained
  behavior, that is a hard implementation boundary.
- If the boundary is not specified, do not invent an external dependency merely
  as a shortcut around implementing the core capability.
- A material new runtime dependency must be made explicit in the plan/contract;
  it must never be introduced silently.
- Follow the user's request and the acceptance contract. Do not reinterpret
  "implement X" as "call a third-party service that implements X" unless that is
  clearly consistent with the requested product.

For acceptance-planning specifically:
- Encode explicit local/offline/external-service requirements as MUST criteria.
- Do not invent a local-only constraint when the user did not ask for one.
- When runtime dependency boundaries are materially relevant but unspecified,
  record that they are unspecified rather than silently choosing one.
<!-- V2.5.6 CORE CAPABILITY BOUNDARY END -->
