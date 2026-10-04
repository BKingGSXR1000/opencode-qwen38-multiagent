# Jupiter R3 reserved worker-helper canonicalization

Fresh original Jupiter R3 reached a complete structured implementation plan but stopped in planning repair. Five browser-feature leaves put worker-authored verification helpers under `.opencode-v2/bin/check_*.py`, even though `.opencode-v2/bin/` is supervisor-owned and the planner prompt already forbids worker ownership there.

The targeted repair repeated the same path mistake across several leaves. This produced a large secondary repair packet and the final planner session then timed out. Adding more prompt text would not make this reliable because the hard rule was already present.

The structured-plan compiler now performs a deliberately narrow canonicalization before semantic validation. For write-capable leaves only, a single-file helper under `.opencode-v2/bin/` is moved to `tests/<basename>` only when the basename begins with `check_`, `test_`, `verify_`, or `validate_` and that exact path is also used by the leaf Verify command. An exact Python helper Verify becomes `python3 tests/<basename>`. Matching references in Outcome and Done-when are updated. `.opencode-v2/bin/run-checks`, production paths, read-only tester ownership, and helpers not used by Verify are never rewritten and continue to fail closed through normal validation.

The untouched archived R3 structured plan was copied into a disposable project and run through the patched compiler and control guard. All five helpers were canonicalized, no repair packet remained, and `IMPLEMENTATION_PLAN.ready` was successfully finalized. Full Python regression suite: 612/612 PASS plus controller, query, sandbox, run-checks, and Node plugin selftests.
