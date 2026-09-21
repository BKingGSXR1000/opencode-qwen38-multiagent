<!-- STAGE_A_PROJECT_MEMORY_BEGIN -->
# OpenCode / Stage-A autonomous project instructions

This repository carries its own authoritative project memory. Before substantial work, read these files in this order:

1. `docs/CURRENT_STATE.md`
2. `docs/ROADMAP.md`
3. `docs/ARCHITECTURE.md`
4. `docs/EXPERIMENT_LOG.md` only when historical evidence is relevant

## Operating mode

Continue the roadmap autonomously. Fix implementation defects, add or repair tests, run the necessary tests, and advance to the next roadmap item without asking for routine implementation decisions.

Do not stop after merely diagnosing a defect when the correct repair follows from `docs/ARCHITECTURE.md` and existing evidence. Implement the repair, test it, update the project state, and continue.

Stop and ask the human only when at least one of these is true:
- two materially different architectural choices remain valid and the repository does not establish which one is intended;
- proceeding would destroy or rewrite retained experimental evidence;
- an action needs credentials, approval, or an irreversible external operation that has not already been authorized;
- evidence contradicts a non-negotiable invariant in `docs/ARCHITECTURE.md`;
- the next step would intentionally change model/context/concurrency/MTP/reasoning configuration rather than merely restore the documented configuration.

The repository and live evidence outrank stale prose. If live evidence proves a project-memory statement wrong, correct the project-memory files in the same coherent change.

## Token / context economy

Be economical with ChatGPT Work context.

Local LLM execution and testing are allowed when they are the correct test. Do not avoid a necessary Qwen/OpenCode run merely to save tokens.

However, do NOT ingest huge logs, database dumps, session histories, generated artifacts, or complete source files when a bounded read can answer the question.

Prefer, in this order:
- exact SQL queries for only the rows/columns needed;
- `rg`/`grep` for an exact event, session ID, error, symbol, or timestamp;
- `tail`, `head`, or bounded `sed -n` ranges;
- `jq` selecting only required JSON fields;
- `git diff --stat`, targeted `git diff -- <file>`, and small contextual hunks;
- hashes, counts, timestamps, and metadata before full contents;
- compare a failing trace against the closest known-good trace and find the first divergence.

Before reading a potentially large output, narrow it first and use a reasonable line/byte cap. Expand only the relevant section if bounded evidence is insufficient. Do not repeatedly reread unchanged large files. Keep short working summaries and reuse exact identifiers already established.

Do not suppress evidence needed for correctness. "Token efficient" means targeted evidence, not guessing.

## Change discipline

- Preserve valid historical runs and attempt accounting.
- Prefer the smallest architectural fix over prompt-only workarounds.
- Run static/component tests before real semantic-worker tests.
- Run the consolidated preflight before any real semantic worker launch.
- Use disposable fixtures/canaries for infrastructure tests whenever retained project state is not the subject of the test.
- If a canary fails, identify the earliest failing boundary before changing another layer.
- Keep `docs/CURRENT_STATE.md` updated after every meaningful milestone.
- Add durable, non-duplicative evidence to `docs/EXPERIMENT_LOG.md`.
- Check off or amend `docs/ROADMAP.md` as work is proven.
- Keep `docs/ARCHITECTURE.md` concise and change it only for deliberate architectural decisions.

## Git

Coherent completed fixes may be committed when useful for safe progress. Do not rewrite or discard valid history. Do not force-push.

Before a commit, run the relevant tests and `git diff --check`. Record the resulting commit in `docs/CURRENT_STATE.md` on the next state update.

Pushing is permitted only when the surrounding task/workflow has already authorized pushing. If that authorization is unclear, commit locally and stop before push rather than interrupting routine implementation work earlier.
<!-- STAGE_A_PROJECT_MEMORY_END -->
