# Qwen3.8 Multi-Agent V2.6.8

Execution:
- Parent is a non-thinking dispatcher; workers own one exact planned Dxxx.
- Preserve DAG-driven concurrency; do not impose an arbitrary two-worker cap.
- The first incomplete compaction may continue; the second retires that child.
- A valid `.opencode-v2/work/Dxxx.ready` always wins over later compaction.
- `execute`/CodeMode is disabled; use direct tools only.

Control-plane authority:
- Filesystem artifacts, not model statements or TodoWrite, decide state.
- Acceptance is complete only with valid `ACCEPTANCE.ready`; planning only with
  valid `IMPLEMENTATION_PLAN.ready`; leaves only with valid
  `.opencode-v2/work/Dxxx.ready`.
- The supervisor solely owns `.opencode-v2/work/attempts.json`; maximum three
  attempts per exact Dxxx. Never create salvage IDs.
- `scripts/control-status.py --project .` is a read-only derived status view.
- Final tests require `TEST_REPORT.json` status=pass and checks_run > 0.
- Exact bare `ACCEPTANCE_PASS` is the only success verdict.

Reference policy:
- Use external research only when the original user request explicitly requires
  external authoritative/reference truth.
- Terms such as correct, accurate, real, scientific, or as seen from Earth do
  not by themselves require external validation.

Acceptance architecture:
1. acceptance-planner defines what must be proven, without research.
2. reference-researcher runs only for `external-required` policy.
3. implementation workers execute the validated DAG.
4. acceptance-validator records executable command/exit evidence; a required
   non-zero validation is FAIL unless a contract-contradictory check is fixed
   and rerun successfully.
5. finalize-acceptance.py creates acceptance-pass.json only on real PASS.

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
