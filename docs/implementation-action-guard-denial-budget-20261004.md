# Bounded repeated action-guard denials

Implementation workers already have a deterministic Early Write/action gate.
Real Qwen sessions showed that a model can nevertheless spend many agent steps
trying different read/bash calls after the plugin has rejected the first
forbidden discovery action. Those rejections are persisted as tool error parts,
not normal completed tool turns, so the existing completed-turn deadline alone
does not bound this recovery loop tightly.

This extension permits two persisted recoverable steering denials in one worker
session. A third attempted tool while the state still requires an owned write,
exact Verify, final return, or explicit contract-challenge return creates the
normal supervisor abort intent and the plugin performs the native interrupt.
The ordinary deterministic retry/split/finalization machinery then decides the
attempt outcome.

Only canonical EARLY_WRITE_IMPLEMENTATION_* steering markers count. Ordinary
test failures, sandbox errors, model text, and unrelated tool errors do not.
Crucially, an owned write or exact canonical Verify is still allowed even after
two prior denials; the denial budget never blocks the required recovery action.
