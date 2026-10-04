# Adaptive medium coding v10: independent results and v11 corrections

Date: 4 October 2026. OpenCode binary stays **pinned at 1.18.31**.

The v10 data are immutable in
`/home/bking/AI/a2-e2e/20261003-adaptive-medium-contract-v10-compare`.
All three modes produced a program that passed five independent external
behavioral tests. Only OBSERVE reached formal Harness Acceptance.

| Mode | Elapsed | Held-out | Final Harness Acceptance | Model-reported input tokens |
|---|---:|---|---|---:|
| OFF | 489.80 seconds | 5/5 | Invalid terminal contradiction despite 5/5 PASS report | 1,249,025 |
| OBSERVE | 679.48 seconds | 5/5 | ACCEPTANCE_PASS, 5/5 | 1,912,035 |
| ENFORCE | 646.65 seconds | 5/5 | D004-B1 exhausted three genuine Verify attempts | 1,975,261 |

Both adaptive modes recorded zero would-interrupt/interrupt transitions;
OBSERVE's maximum periodically sampled action age was 6.85 seconds and
ENFORCE's was 7.52 seconds. No causal watchdog performance comparison or
false-positive rate can be inferred from these single, stochastic coding
runs. They consume the same existing local vLLM backend.

OFF's validator made exactly one accepted write of a complete 5/5 PASS
`acceptance-report.json`, with each Axxx's exact observed successful Verify
command attached. Its later final answer nevertheless began ACCEPTANCE_FAIL,
incorrectly claiming an A002 semantic contradiction despite the passing
full-range immutable priority-band tests and source code. The supervisor
correctly refused to silently turn this contradictory terminal answer into
a verified pass, so OFF did not receive an acceptance-pass marker. The generic
validator role and controller prompt now require ALL evaluation and evidence
reconciliation before the immutable first write, then an exact terminal
response matching that report. A dedicated regression asserts that a
terminal FAIL following a first PASS cannot produce a silent success.

ENFORCE's final worker-created tests used priorities such as `"high"` and
`"normal"` as *inputs*, contradicting the task's strict integer 1–5 input
contract. The correct `cli.py` consequently rejected them. D004-B1's three
Verify attempts failed, and the controller properly ended at a bounded
attempt-limit blocker. The previous immutable worker-test contract verified
only the existence of a real subprocess test, not the correctness of its test
vectors. New benchmark-author-owned `spec_tests/reference/` contains an
independent known-good standard-library implementation. An additional
immutable contract test now copies the *worker-authored* test suite and this
reference implementation to a temporary directory, runs their real
subprocess tests with isolated PYTHONPATH, and rejects any test suite that
fails against the known-good implementation. This is a **benchmark-specific
quality oracle**, not a general claim that every repository has a reference
implementation or that correct tests must match particular source code.
Its reference files are SHA-bound in the original trusted seed manifest.
Tests exercise both a valid worker test and v10's invalid string-priority
fixture. No model-authored application code was manually edited.

The v11 benchmark is intentionally a new isolated, non-overwriting project,
with private ports 58580–58582 and the same five-leaf parallel/final test
contract. Strict sequential gating starts OBSERVE only after OFF achieves
both fully independent 5/5 Acceptance and held-out PASS, and ENFORCE only
after OBSERVE achieves both. A failed mode is archived and stops the next
runs; the shared vLLM and pinned OpenCode installation remain untouched.
