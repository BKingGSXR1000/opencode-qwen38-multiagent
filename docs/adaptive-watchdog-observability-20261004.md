# Adaptive reasoning watchdog observability

Real Qwen medium-coding runs on the current vLLM/OpenCode path expose normal
text and tool parts but no reasoning parts. Project-scoped v10 OBSERVE,
v10 ENFORCE and current v11 OBSERVE telemetry therefore report zero visible
reasoning characters since action. A zero candidate count in such a run is
not evidence that the adaptive reasoning thresholds are well calibrated:
the reasoning-budget policy had no positive visible reasoning signal to score.

The benchmark and watchdog report now distinguish:
- reasoning-budget-evaluable: at least one positive visible reasoning sample;
- no-visible-reasoning-signal: adaptive decisions existed, but every observed
  reasoning_chars_since_action value was zero.

Thresholds are intentionally unchanged. Implementation workers are still
protected by the separate deterministic Early Write/action state machine:
bounded state-preserving reads, direct owned writes, exact Verify, and
session retirement at the configured S/M tool-turn ceiling. The legacy SSE
text cap remains a separate fail-safe.

For this local Qwen nothink path, action/tool compliance is the meaningful
primary loop-control evidence. Adaptive reasoning thresholds should only be
evaluated on a model/runtime that actually emits typed reasoning parts, or
after a separately designed and tested text-channel policy that avoids
mistaking legitimate final responses for deliberation.
