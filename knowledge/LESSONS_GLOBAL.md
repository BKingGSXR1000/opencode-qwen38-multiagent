<!-- V2.6.6 GLOBAL LESSON SEED -->
# Global Harness Lessons

These are reusable operational lessons. `active` entries are strong defaults.
`candidate` entries are advisory until repeated evidence supports promotion.

## G001 — Durable completion outranks later compaction
Status: active
Area: completion
Lesson: If verified work is durably marked complete before automatic compaction,
the later compaction is not task failure.

## G002 — Final implementation leaves must be S/M
Status: active
Area: planning
Lesson: L/XL leaves must be recursively decomposed before dispatch.

## G003 — Unknown dependency APIs get executable probes
Status: active
Area: dependency
Lesson: Freeze exact installed API/import/runtime behavior in a small probe leaf
before downstream workers depend on it.

## G004 — Launch dependencies are hard barriers
Status: active
Area: scheduling
Lesson: Never speculatively launch a leaf before all Launch deps are complete.

## G005 — Externalize difficult reasoning incrementally
Status: active
Area: worker
Lesson: Prefer short reason->tool->inspect cycles and durable progress over long
hidden reasoning followed by one giant write.

## G006 — Numerical work belongs in executable checks
Status: active
Area: worker
Lesson: Use scripts/tests for numerical derivation, calibration, and comparison.

## G007 — Recovery may not weaken MUST requirements
Status: active
Area: acceptance
Lesson: Narrow implementation work if needed, but preserve the original accuracy,
behavior, and acceptance obligation.

## G008 — Non-thinking worker is the default
Status: active
Area: routing
Lesson: Use reasoning-builder only when the leaf genuinely needs deep algorithmic
reasoning; ordinary implementation should be tool-oriented and non-thinking.

<!-- V2.6.7 GLOBAL SEED -->
# Global Harness Lessons
- G001 active: verified durable completion outranks later automatic compaction.
- G002 active: final implementation leaves must be S/M.
- G003 active: unknown dependency APIs get executable probe leaves.
- G004 active: Launch dependencies are hard scheduler barriers.
- G005 active: difficult workers externalize progress in short reason->tool cycles.
- G006 active: numerical/algorithmic work should use executable checks.
- G007 active: recovery may not weaken MUST requirements.
- G008 active: non-thinking implementation is default; reasoning-builder exceptional.
- G009 active: one incomplete automatic compaction is warning; repeated compaction stronger failure evidence.
- G010 active: intended tests run as independent subprocesses with explicit report.
