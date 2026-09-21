# Generic deterministic Stage-A run

Stage-A is project-neutral. It stores the complete requested task in the target
project, then uses the deterministic controller to choose every subsequent
phase. The technical root is `transport-root` on `v2noop/root-noop`; semantic
agents are created only as native children of that root.

Create a UTF-8 task file containing the complete request, export the local
model key, then run:

```bash
/home/bking/AI/opencode-qwen38-multiagent-v2-a2-v11831/scripts/start-stage-a-run.sh \
  /absolute/path/to/any-project \
  /absolute/path/to/task.md
```

Use `--preflight` before a real run to verify the project, task file, and
canonical transport configuration without changing the project or starting any
runtime process.

For a bounded integration check, add `--max-ticks 1`; ordinary runs omit that
option and continue until acceptance or a durable blocker.

The launcher starts the canonical local runtime, a project-scoped supervisor,
and a fresh pinned technical root. It passes that root explicitly to the quiet
deterministic driver rather than relying on ambient session selection.
The driver prints only phase changes, not child model transcripts, and stops at
either `ACCEPTANCE_PASS` or a durable blocker.

The legacy `/oneshot*`, `/team*`, and `/resume*` OpenCode commands are retired:
they no longer select the LLM orchestrator. Do not use them to start a run.
