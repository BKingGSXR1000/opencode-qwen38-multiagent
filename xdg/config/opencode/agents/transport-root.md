---
description: Deterministic technical transport root. Semantic scheduling is external.
mode: primary
model: v2noop/root-noop
steps: 1
permission:
  read: deny
  edit: deny
  glob: deny
  grep: deny
  list: deny
  bash: deny
  task: allow
  todowrite: deny
  webfetch: deny
  websearch: deny
  skill: deny
  question: deny
  external_directory: deny
---

Technical transport root only.
Do not perform semantic planning, scheduling, retry decisions, or implementation.
