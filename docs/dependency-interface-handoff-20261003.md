# Verified cross-agent interface handoffs: October 3, 2026

Purpose: improve the general deterministic OpenCode V2 coding harness. The
record-summarizer is a disposable multi-worker benchmark, not a product.

## Initial diagnosis

A five-deliverable project starts D001 and D002 concurrently, integrates
their APIs in D003, adds CLI/tests/docs in D004, then verifies final tests in
D005. In the weak original fixture, syntax-only Verify marked two incorrect
modules READY: invalid normalization types raised TypeError rather than
ValueError, and the priority classes were reversed. D003 incorrectly assumed
different upstream export names and raised an unsupported contract challenge.
The weak OFF run archived after 392.08 seconds without accepted completion or
external functional success; no other modes were started.

An immutable trusted Verify fixture, authored before model execution under
spec_tests/, corrected the first false-positive problem. Its first two
modules passed external behavioral Verify. Yet the generic D003-context.json
still conveyed only dependency filenames, not verified exported symbols.
D003 guessed imports and exhausted the second strict OFF reference run
(422.1 seconds). Neither diagnostic baseline should be compared as a
healthy watchdog-mode performance result.

## New generic handoff

scripts/dependency_interfaces.py uses bounded, non-executing Python AST
inspection on currently READY direct dependency source files. It publishes
public top-level function/class names, exact relative source paths,
SHA-256 source hashes, and bounded guarded upstream outcome/done-when
metadata. It rejects missing/unverified dependencies, absent current attempt
ledgers, symlinked files and parents, path escapes, invalid syntax or UTF-8,
and oversized sources. The packet has no original model reasoning, arbitrary
source bodies or execution of upstream project code.

scripts/control_query_views.py now includes verified_dependency_interfaces
alongside its previous dependency-file list in each downstream leaf packet,
publishing it before the eligible-task decision. A newly eligible D003
therefore receives an already checked API list and source provenance.
scripts/control_policy.py includes the new Python dependency module in the
runtime reload fingerprint. Unlike a merely historical READY file, the
interface requires control_state.ready_info against the current manifest
and authoritative attempt ledger. The policy/source revision changes the
control-policy fingerprint; earlier archived acceptance proofs retain their
original tested-version provenance.

A fresh strict OFF experiment at
/home/bking/AI/a2-e2e/20261003-adaptive-medium-handoff-compare/off
confirmed D001 and D002 READY in one attempt each and published the
expected normalize_name and priority_band names and hashes into the
D003 context. The D003 agent actually read that exact packet and then
imported both correct names. Function discovery was repaired, but the
model's first and second integrations still failed trusted semantic tests
(e.g., treating a string priority class as a numeric threshold); the
controller correctly booked two genuine Verify failures and initiated
automatic recursive decomposition. This is an independent remaining
model-coding issue, not evidence that API handoff or functional Verify failed.

## Safety and evidence

Three direct-interface tests, two packet-integration/runtime-fingerprint
tests, and one missing-ledger regression cover the new component, including
source hash, public-name filtering, exact verified-only claims, safe paths,
and absent or corrupted pre-plan state. The complete harness suite at
this integration point passed 547 of 547 tests, together with controller,
query-view, sandbox and native OpenCode plugin selftests.

The OFF/OBSERVE/ENFORCE medium comparison is gated: later modes cannot
start unless the fresh strict OFF contract independently passes both its
five-MUST harness acceptance and separately authored external held-out
tests. The early weak and earlier strict diagnostic failures remain
separate archives and are never averaged into healthy timings.

Potential next generic changes: bounded AST-derived return literal/type
metadata clearly labeled as syntactic source observations, stronger
fact checking of objective Contract Challenge assertions, and
correction prompts tied to failed exact Verify output. These are future
design questions; do not claim they are already implemented.

## Final archived medium-handoff outcome

The fresh OFF run completed its bounded 390-second execution window and
archived a result after 392.11 seconds. The **independent held-out grader
passed all five functional tests** on the generated Python package, including
the exact CLI JSONL result, normalization errors, strict priority boundaries
and input immutability. That evidence establishes working application
behavior on those held-out vectors without a human modifying the application.

However, the deterministic Stage-A controller still reported execution,
not final Acceptance: the benchmark timed out just after dispatching D005
(test-builder) and before the final TEST_CHECKS/Acceptance-validator chain
could complete. Do NOT call the off-mode run ACCEPTANCE_PASS. Its ledger
records D001=1 clean, D002=1 clean, D003=2 genuine Verify failures,
D003-A=1 diagnostic, D003-B=1 corrective, D004=1 and D005=1
incomplete at the deadline. No infrastructure retry credits were granted.
The recorded sequential comparison correctly refused to start either
OBSERVE or ENFORCE because full OFF Acceptance and held-out tests were
both required.

The outcome supports a limited generic inference: exact verified API
handoff eliminated missing-name imports and allowed recursive semantic
repair to produce code passing five independent held-out checks, whereas
both earlier weak/no-interface diagnostic runs failed held-out quality.
It does NOT establish a relative speed or correctness ranking of the
watchdog modes, nor prove the source of each improvement causally because
these are separate stochastic model runs.

Next acceptance experiment must either allocate a longer bounded window
for the five-leaf task or explicitly exercise crash-safe continuation
from the exact current ledger and newly launched D005 session after a
server restart. A terminal pass requires the independent canonical
Acceptance marker and held-out checks, not held-out quality alone.
