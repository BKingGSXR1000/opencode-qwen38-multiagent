# V2.2 Qwen3.8 model policy

## Dispatcher
Non-thinking mode.
It only maps user goals/results to subagent calls. Long reasoning here is pure
overhead and previously caused compaction before the first useful delegation.

## Heavy builders / integrator
Thinking remains enabled at medium reasoning effort.
Historical thinking is NOT preserved between turns.

## Tester / reviewer / investigator / state writer
Thinking remains enabled at low reasoning effort.
Historical thinking is NOT preserved between turns.

## Why preserve_thinking=false?
The durable state of a coding agent is the filesystem, test results, and compact
handoffs. Replaying every historical thinking block consumes context/KV without
being required for project continuity.
