# Adaptive watchdog healthy-worker comparison — 3 October 2026

## Controlled OFF / OBSERVE / ENFORCE smoke test

Tested integration base: 14cb531. Runner: scripts/benchmark-adaptive-healthy.py.
This was one disposable two-deliverable Stage-A coding project per mode, not
an application product. Each project had a dedicated localhost OpenCode server
(port 58541 / 58542 / 58543), exact-project supervisor, independently pinned
no-Qwen transport root, same Qwen vLLM model server, independent preflight,
the same precompiled and guarded plan, and the same exact final Verify.

All three ran SEQUENTIALLY, never competing directly with one another. They
did still share the vLLM backend with unrelated pre-existing project activity,
so the experiment cannot isolate performance effects from queueing or model
trajectory variability.

| Metric | OFF | OBSERVE | ENFORCE |
|---|---:|---:|---:|
| Independent Acceptance MUSTs | 2/2 | 2/2 | 2/2 |
| Elapsed wall seconds | 72.65 | 85.03 | 71.53 |
| OpenCode sessions | 4 | 4 | 4 |
| Tool-call parts | 19 | 24 | 17 |
| Completed tool-call parts | 10 | 11 | 10 |
| Model-reported input tokens | 128,301 | 184,636 | 126,723 |
| Model-reported output tokens | 1,902 | 2,123 | 1,876 |
| Adaptive candidate events | n/a | 0 | 0 |
| Confirmed adaptive interrupts | 0 | 0 | 0 |
| D001 and D002 attempts | 1 each | 1 each | 1 each |
| Genuine failures or infrastructure credits | 0 | 0 | 0 |

Source data: /home/bking/AI/a2-e2e/20261003-adaptive-healthy-compare/summary.json
plus the off, observe and enforce subdirectories' result.json, project
acceptance evidence, driver logs, preflight receipts and controller ledgers.
Each private server and supervisor was shut down after its independently
verified acceptance; none of its three private OpenCode ports remain open.
Shared vLLM remained untouched.

OBSERVE produced 11 periodic policy-decision telemetry records with
maximum sampled action age 1.20 s. ENFORCE produced nine with maximum
sampled action age 1.18 s. There were no positive watchdog candidates
or interrupts; all attempts succeeded on the first try. This proves only
smoke-level compatibility on a small healthy workflow, NOT a reliable
production false-positive rate.

The model-reported reasoning-token counter was zero in all three runs.
Do not infer that the model had no private reasoning, or that native SSE
emitted none: short bursts between five-second telemetry polls can be missed.
The larger input token variation (128k vs 185k vs 127k) shows model/tool
trajectories differed between runs. These single-run wall times cannot
establish a causal speedup or slowdown due to adaptive watchdog mode.

## Benchmark implementation and safety

The committed runner creates one isolated exact-project server overlay and
project-scoped supervisor per mode; it validates runtime source identity,
the supervisor's actual mode in its process environment, preflight and
independent acceptance; measures wall time, native OpenCode session/part
and model-message totals, project-scoped watchdog telemetry, retries and
failure classifications; archives JSON receipts; and refuses to overwrite
any earlier mode directory. It does not start or kill the shared model server.

Four new unit tests verify unique private ports, refuse destructive reruns
of an existing mode, independently meter synthetic SQLite records from
the exact requested project, and refuse to send signals to a foreign
supervisor or server. Run all harness regression and existing guard tests
before merging the runner into integration.

## Follow-up

Test the same larger, genuinely multi-file/multi-worker coding contract
several times across modes, with matched backend load, independent functional
test vectors, exact Verify, per-worker action-age distribution and GPU
queueing measurements. Keep the previously validated real native
OpenCode synthetic-stall enforcement test as the efficacy proof; never
extrapolate its deliberately adversarial stall into ordinary coding.
Production adaptive watchdog mode stays OFF until stronger evidence.
Separately compare long-horizon no-memory, regular compaction, structured
persistent memory and selective retrieval under matched workloads.
