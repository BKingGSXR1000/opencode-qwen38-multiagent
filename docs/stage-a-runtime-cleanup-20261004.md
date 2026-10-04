# Stage-A private runtime cleanup

Fresh Jupiter R1-R5 runs showed that after the Stage-A driver terminalized, each run's private OpenCode server wrapper, OpenCode serve child, and project-scoped supervisor could remain alive for hours. These stale runtimes consumed CPU and competed with later Jupiter runs for the same local Qwen backend.

The cause was in `start-stage-a-run.sh`: it launched the private server and supervisor, then replaced itself with `exec drive-stage-a-run.py`. Once the driver exited there was no launcher process left to clean up resources.

The launcher now retains ownership of the lifecycle. It captures the exact server-wrapper PID and supervisor PID, runs the driver as a child, waits for and returns the driver's exit code, and performs project-scoped cleanup from an EXIT trap. Supervisor cleanup verifies the exact `V2_PROJECT` environment before signaling. Server-wrapper cleanup verifies the exact launcher command including project and port. TERM/INT are forwarded only to the current driver and then normal EXIT cleanup runs. The shared vLLM backend and shared no-op model service are not touched.

Regression coverage verifies shell syntax, PID ownership, cleanup traps, absence of the old exec replacement, signal forwarding, and preservation of the consolidated preflight-before-driver invariant. Full Python suite: 634/634 PASS plus controller/query/sandbox/run-checks and Node plugin selftests.
