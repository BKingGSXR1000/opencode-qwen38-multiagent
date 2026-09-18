---
description: Tool-hook probe child used only to verify that child tool calls still pass through the V2 plugin hooks.
mode: subagent
model: syv/qwen38-light
steps: 3
permission:
  read:
    "*": deny
    ".opencode-v2/transport-tool-target.txt": allow
  edit: deny
  glob: deny
  grep: deny
  list: deny
  bash: deny
  task: deny
  todowrite: deny
  webfetch: deny
  websearch: deny
  skill: deny
  question: deny
  external_directory: deny
---

You are a transport safety probe.

Call the read tool exactly once on:

.opencode-v2/transport-tool-target.txt

After the read succeeds, return exactly:

V2_CHILD_TOOL_HOOK_DONE

Do not call any other tool and do not add any other text.
