# Selective persistent project memory — controlled A/B plan

This benchmark isolates one experimental factor: the opt-in
.opencode-v2/work/selective-memory.enabled marker.

Both variants keep OpenCode pinned at 1.18.31, adaptive reasoning OFF, the same
local Qwen backend, identical five-leaf verified task, immutable behavioral
specs/reference oracle, exact-Verify policy, independent held-out grader, and
the same acceptance finalizer. The ON variant writes the opt-in marker before
canonical fixture bootstrap.

Metrics retained per run include formal Acceptance, held-out result, elapsed
time, sessions, native tool calls, genuine retries, model-reported tokens,
memory events, verified checkpoints, history bytes, and bounded retrieval
packet sizes.

The first OFF-to-ON pair is exploratory only. Qwen sampling is stochastic and
shared-backend load can vary. Any causal claim requires repeated alternating
orders and reporting failed runs, not selecting successful trajectories.

No project transcript is promoted to authority. Memory stores canonical
Acceptance/plan revisions, audited attempt/recovery diagnostics, and current
supervisor-validated READY checkpoints. Historical records remain historical.
