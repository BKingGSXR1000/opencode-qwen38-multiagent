# Jupiter R5 numeric Verify/Done-when contract mismatch

Fresh original Jupiter R5 progressed through planning and into real implementation. D001 and split child D002-A reached READY. The remaining D002-B writer failed twice, then recursive split recovery identified a real parent contract mismatch: Done-when required at least 4 fixture files, while the canonical Verify asserted `len(files) >= 5`.

The runtime splitter correctly described that mismatch, but deterministic parent-contract validation could not prove it because Verify adequacy had no numeric file-count consistency check. This caused split-validation-failed instead of a targeted plan repair.

`leaf_contract.validate_verify_adequacy()` now contains one deliberately narrow check: Done-when must explicitly say `at least N ... files`; Verify must be an actually parsed Python `-c` assertion of `len(<file-like-variable>) >= M`; only variables whose name contains `file` or `fixture` are considered; if M differs from N, Verify is rejected as stricter or weaker than Done-when. Arbitrary numeric requirements and unrelated `len(items)` expressions are ignored.

The untouched archived R5 structured plan was replayed through the patched compiler/control guard. It now fails before implementation with a targeted repair packet affecting only `ephemeris_fixtures_probe`, reporting that Verify minimum 5 is stricter than Done-when minimum 4. No `IMPLEMENTATION_PLAN.ready` is created. Full Python suite: 630/630 PASS plus controller/query/sandbox/run-checks and Node plugin selftests.
