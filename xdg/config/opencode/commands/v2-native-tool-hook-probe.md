---
description: Verify child tool calls still traverse plugin execute hooks.
agent: transport-tool-probe
---

Call the read tool exactly once on `.opencode-v2/transport-tool-target.txt`.

After the read succeeds, return exactly:

V2_CHILD_TOOL_HOOK_DONE

Do not call any other tool and do not add any other text.
