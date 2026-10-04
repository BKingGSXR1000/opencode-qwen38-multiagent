# Bounded AST return-literal hints for verified dependency handoffs

This extension is prepared in a **separate worktree** while the fixed
OpenCode 1.18.31 integration worktree runs the unchanged v11 benchmark.

The existing source-bound, exact-READY dependency interface projection
already supplies public Python function/class names and source SHA-256.
For direct dependencies only, this extension additionally records short,
strictly restricted string literals from direct return statements and both
branches of conditional return expressions. It never executes project
code, interprets free-form docstrings, descends into nested definitions,
or claims those literal observations are demonstrated behavior.

Each projected source includes
`literal_hint_authority: static-AST-observation-not-verified-behavior`.
Examples of valid hints: a function with
`return "high" if value >= 4 else "normal"` can advertise both `high`
and `normal` as **possible source literals**. Empty hints mean nothing
about a function's actual runtime return type. Hidden branch conditions
and dynamic expressions are deliberately not inferred.

The parser accepts only bounded, ASCII-like identifier strings up to
32 characters; no sentence/instruction text may enter this field.
The existing symlink, traversal, oversize, UTF-8, direct-dependency and
actual READY/attempt-ledger checks are unchanged. Unit regressions
cover conditional values, dynamic results, nested functions/classes,
and attempted natural-language injection.

If adopted into integration, the new source fingerprint automatically
changes because dependency_interfaces.py is a tracked runtime-policy
source. Existing running canaries must finish before integration.
