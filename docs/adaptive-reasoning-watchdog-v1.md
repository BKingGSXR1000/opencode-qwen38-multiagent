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
