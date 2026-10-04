# Jupiter R6 Node service Verify ternary exit

Fresh original Jupiter R6 reached implementation planning and repaired its initial dangling dependency errors. It then stopped after planner retry exhaustion because control-guard rejected two otherwise executable service checks (`server` and `readme`). Both checks actually launch `server.js`, poll a localhost HTTP URL with `fetch`, record the response status, and fail closed with `process.exit(code===200?0:1)`.

The existing Node embedded-service detector already required a real spawn, a localhost fetch, and fail-closed behavior, but only recognized `throw new Error(...)` or a literal non-zero `process.exit(1)`. It therefore produced a false positive for the status-dependent ternary exit.

The detector now also accepts a narrow fail-closed ternary form when `process.exit` branches between 0 and a positive non-zero code and the condition explicitly references a `code` or `status` value. A constant condition such as `process.exit(true?0:1)` is still rejected. Spawn-only, fetch-only, static-string, and invalid-JavaScript negative cases remain rejected.

Both exact archived R6 Verify commands now pass `embedded_node_server_http_verify` and `validate_verify_adequacy`. Replaying the untouched final R6 plan with the patch yields no repair packet. Full Python suite: 632/632 PASS plus controller/query/sandbox/run-checks and Node plugin selftests.
