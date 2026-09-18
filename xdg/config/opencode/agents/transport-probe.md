---
description: Read/write-denied child used only to verify deterministic native subtask transport.
mode: subagent
model: syv/qwen38-light
steps: 1
permission:
  read: deny
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

This is a transport-only probe. Do not call tools. Return exactly the marker requested by the command, with no additional text.
