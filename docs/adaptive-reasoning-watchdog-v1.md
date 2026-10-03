# Adaptive reasoning watchdog v1 (Stage-A, opt-in)

The existing supervisor already has a backend-aware SSE no-progress timer,
an invisible-stream fallback, a hard per-response reasoning-character ceiling
(20,000 for ordinary workers), and special planner retirement rules. Do not
replace them. This module adds an **independent tool-action clock**, because
streamed thinking tokens should not indefinitely reset a no-useful-action
watchdog.

## Deployment

Set V2_ADAPTIVE_REASONING_MODE when starting the project supervisor:

- off: default; no new decisions or interrupts
- observe: record a deduplicated ADAPTIVE_REASONING_WOULD_INTERRUPT event and
  emit the adaptive decision in existing supervisor telemetry; never interrupt
- enforce: use the ordinary durable supervisor abort-intent and native OpenCode
  interrupt. An HTTP interrupt failure does not mark the session as retired:
  the next tick may retry. Operator grants are not created by this feature.

Start in observe. Enforce only in a disposable canary after a representative
set of successful and failing runs has been reviewed. The mode is read once
on supervisor startup and the new module is covered by the supervisor-runtime
fingerprint so a long-running process reloads it safely.

## Budgets

The source of complexity is each leaf's current guarded implementation
manifest. No additional LLM classifier is needed. Profiles apply only to
implementation agents; the independent planner, validator, orchestration root
and reference agent retain their existing supervision.

Profile | Reasoning-character budget | Minimum action age | Action-age bound
--- | ---: | ---: | ---:
TRIVIAL | 3,600 | 45 s | 90 s
NORMAL | 6,500 | 75 s | 180 s
COMPLEX | 14,000 | 120 s | 300 s
RESEARCH/probe | 18,000 | 180 s | 480 s

A budget breach requires BOTH minimum time and enough observed reasoning since
the last action. At the age bound, at least half the profile's character budget
must still be present. Three identical, nontrivial 120-character reasoning
blocks permit earlier intervention only after 30 s and a minimum of visible
reasoning. A running tool, active compaction, missing current assistant
message/parts, or zero observed reasoning suppresses this new guard.

The clock resets on current assistant message/tool-ID changes, completed
SSE tool actions, interrupted tool calls, and SSE counter resets. It does
NOT reset merely because another reasoning/text delta streamed. This is
deliberately separate from the old SSE progress watchdog, which must continue
to protect long-running argument generation and backend stalls.

These thresholds use **visible streamed characters**: partial per-request
hidden thinking-token accounting is not reliably available in this OpenCode
runtime. They do not change the proxy's existing hard per-role model thinking
or max-generation token budgets. Shared vLLM progress is global, not proof
that a particular child has progressed. It is reported for diagnostics but
does not override this action clock.

## Telemetry and backward compatibility

Supervisor event and CSV names:
- ADAPTIVE_REASONING_WOULD_INTERRUPT, observe only
- ADAPTIVE_REASONING_INTERRUPT, enforce and HTTP interrupt succeeded

The existing five-second supervisor telemetry row adds
adaptive_reasoning_mode and adaptive_reasoning with profile, gate,
action_age, reasoning_chars_since_action, budget and coarse backend state.
It never includes the actual chain-of-thought text. A bounded in-memory tail
is used solely to recognize exact repeated spans; it is cleared after tools.

The old watchdog telemetry writer emitted literal backslash-n delimiters,
producing a 17 MB concatenated JSON document log instead of JSONL. New writes
use actual newline delimiters. scripts/watchdog-report.py uses a JSONDecoder
offset parser and continues to read both legacy and new records without
rewriting the historical evidence, including JSON strings containing text
that looks like an object delimiter.

## Validation and limitations

15 policy-focused tests cover adaptive classification, independent action
age, SSE counter/tool/reset lifecycle, identical-span loop recognition,
threshold combinations, backend-queued false-positive avoidance, active-tool
and compaction suppression. Three integration tests cover off, observe-once,
and enforce/retry after an HTTP interrupt failure. Four format tests cover
legacy and true JSONL telemetry. Run the complete harness regression suite
and all controller/sandbox/plugin checks before committing.

Historical offline replay of 14,325 available telemetry records across 520
sessions found no candidate interventions on either completed acceptance
canary under conservative reset heuristics. Historical rows omit exact
last-tool IDs; therefore this replay is not a reliable false-positive rate
or proof of efficacy. Complete a separate live observation canary with
implementation workers before enabling enforcement.

NEXT: a matched long-horizon quality/speed comparison with and without
selective project memory, then tightly bounded enforcement testing with
fault-injected reasoning loops and false-positive metrics. No Jupiter product
implementation is included: all application tasks are disposable harness tests.

## Verified observation and action-clock continuation (2 October 2026)

The isolated tasklog memory-enabled canary at
`/home/bking/AI/a2-e2e/20261002-adaptive-watchdog-observe-canary/project`
reached independent `ACCEPTANCE_PASS`: all 23 MUST criteria passed, 13
supervisor-verified current READY checkpoints and 70 selective-memory events.
Its project supervisor had `V2_ADAPTIVE_REASONING_MODE=observe` set. The corrected
legacy/new JSON decoder recovered 289 observation records from 36 sessions
in this run; no would-interrupt event or actual adaptive interruption occurred.
Implementation-worker samples mostly contained no visible reasoning at the
five-second polling points. Four large visible reasoning samples belonged to
the implementation planner, intentionally governed by its existing separate
retirement policy. The live run establishes compatibility, **not** an
empirical false-positive rate for enforcement.

A subsequent action-clock fix persists `action_reasoning_chars` across
`session.next.step.started` events that have no tool invocation and keys a
connected SSE session's action timer by actual `action_seq` and successful
tool count rather than assistant message ID. A reasoning-only continuation
therefore cannot evade the budget simply by starting a new message. On a
tool invocation, completion or failure, the action counter resets. If SSE is
unavailable, the earlier conservative per-message clock is retained. Two
additional tests cover multi-step reasoning without actions and live-clock
stability across message-ID rollover. Enforce mode remains opt-in and has
not yet been used to interrupt a real Qwen child.
## Native OpenCode v1 SSE and controlled enforcement proof — 3 October 2026

The early observer canary was limited by an overlooked wire-format mismatch:
OpenCode v1.18.31 emits `message.part.delta` with `field=text` for BOTH
reasoning and ordinary text; the earlier watcher only processed
`session.next.reasoning.delta`. Therefore missing reasoning values in the
earlier observer trace must not be interpreted as genuine no-reasoning
behavior or a measured false-positive rate.

The supervisor now maps native `message.part.updated` part IDs to canonical
reasoning/text/tool/step types. For a late SSE subscription, it can resolve a
part ID with a **read-only, same-session** query against the native OpenCode
`part` table. Native reasoning and text deltas are kept separate; each real
tool invocation, completed tool or failed tool produces the corresponding
watchdog action. It never treats arbitrary text as thinking, and an event
bound to another session is rejected. The cumulative action clock continues
across model-only step changes. An SSE disconnect clears the connection
indicator; the fallback stays conservative.

Two independent fresh canaries were retained. Both used an isolated compiled
and guarded two-deliverable Stage-A fixture, native no-Qwen technical root,
their own localhost OpenCode server and a **one-shot synthetic** OpenAI-compatible
SSE stall proxy. Only the first worker-model call was intercepted. All later
model calls were routed to the existing local Qwen proxy; the shared vLLM
server and previously accepted projects were never reconfigured or killed.

- Baseline, BEFORE native normalization:
  `/home/bking/AI/a2-e2e/20261003-adaptive-enforce-sse-fault/project`.
  The old hard watchdog terminated the unobservable stream after about
  120 seconds and 64 synthetic frames. It correctly recorded one
  `immediate-runtime-cancel` infrastructure abort and allowed recovery.
  The resumed Qwen worker, final test worker and independent validator
  produced valid 2/2 `ACCEPTANCE_PASS`. This is a historical control, NOT
  evidence of an adaptive interrupt.
- Native bridge plus adaptive ENFORCE:
  `/home/bking/AI/a2-e2e/20261003-adaptive-enforce-native-v1/project`.
  The native SSE adapter immediately observed the synthetic reasoning
  deltas and measured increasing reasoned characters and action age.
  Profile NORMAL has an earliest intervention time of 75 seconds and a
  6,500-visible-character budget. At precisely **75.3 seconds** and
  **11,778 visible reasoning characters since action**, the project-scoped
  supervisor emitted `ADAPTIVE_REASONING_INTERRUPT` and called the real
  OpenCode HTTP abort endpoint. The fault proxy observed the upstream stream
  disconnect after 41 frames. The durable supervisor abort-intent record
  became resolved as `genuine-worker-behavior-failure`; D001 attempt 1 was
  recorded as one genuine failure, with **zero infrastructure credits**.
  The ordinary Stage-A controller dispatched D001 attempt 2 to actual local
  Qwen. D001 and D002 became supervisor-READY and the independent
  acceptance-validator reached valid 2/2 `ACCEPTANCE_PASS`, no blockers.
  No test-application files were manually modified.

The live test revealed another diagnostic weakness: a short adaptive trigger
may occur between the standard five-second telemetry samples. A subsequent
change now emits a `force=True` JSONL record at every distinct observed
would-interrupt or successfully confirmed interrupt. The record contains
the bounded policy verdict, role, deliverable, project binding and coarse
backend state, but **never reasoning text or model prompts**. If the telemetry
file is unavailable, the already-confirmed abort and durable ledger are
unaffected. This last event-emission refinement is covered by deterministic
tests; the accepted ENFORCE canary was run immediately before it was added.

The project-scoped test proxy and both private OpenCode/supervisor instances
were stopped only after their acceptance markers were independently verified.
The local Qwen model server was not interrupted. Default production mode
remains `off`; the two canaries prove targeted interrupt and bounded
recovery under an adversarial **synthetic** stall. They do not establish
production false-positive rates against representative complex real work.
Next compare healthy workload outcomes and costs in OBSERVE versus controlled
ENFORCE, and no-memory versus selective-memory context retrieval.
