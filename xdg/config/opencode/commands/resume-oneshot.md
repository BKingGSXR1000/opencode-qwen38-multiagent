---
description: Retired legacy command. Continue with scripts/drive-stage-a-run.py.
agent: transport-root
---

RESUME AND FINISH the unfinished project in the CURRENT directory.

Original/remaining goal:
$ARGUMENTS

This is a FRESH parent session because the prior parent context became overfull or stalled.
Files on disk are authoritative completed work. Do NOT reconstruct or repeat the old conversation.

MANDATORY:
1. Use at most one small read/glob/grep batch to identify what exists, what is runnable,
   what is unfinished, and safe ownership boundaries.
2. Immediately launch core-builder, feature-builder, and tester in parallel where applicable.
3. The orchestrator MUST NOT edit, shell, test, or web-search.
4. Editing workers need explicit non-overlapping ownership.
5. Keep parent reasoning short. Never spend a response merely re-planning.
6. When workers finish, launch integrator to combine/fix and prove it runs.
7. Then launch reviewer. Delegate material fixes and retest.
8. Continue additional waves until all known MVP functionality is present, the project runs,
   important behavior is validated, material findings are fixed, and no required work remains.
9. Do NOT stop because of checkpoint, compaction, summary, milestone, or completed agent wave.
10. Do not git push unless explicitly requested.
