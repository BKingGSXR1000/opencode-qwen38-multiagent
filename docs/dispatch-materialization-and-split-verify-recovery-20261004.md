# Dispatch materialization race and split tester Verify recovery

A native implementation dispatch persists its controller execution intent
before prompt_async transport. OpenCode child creation and the supervisor
preclaim can become visible slightly later. The controller now suppresses an
exact same-execution replay for a bounded 12-second materialization window.
It returns a replay-suppressed transport-pending receipt and never emits a
second child. After that grace, absent preclaim/child evidence remains a
fail-closed AMBIGUOUS_EXECUTION.

Verification-recovery writer-to-tester splits also statically reject Python
unittest discover commands with an explicit top-level when the selected start
directory is not importable below that top-level. A read-only tester cannot
repair such a command by creating package markers, so accepting it would create
a deterministic dead-end leaf.
